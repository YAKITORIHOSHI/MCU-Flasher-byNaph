# Migration changes

## 18 October 2026 — Actual code implementation

- Added parallel Rust workspace with contracts, headless project/pure services,
  native host probes and Tauri 2 fixture application. Production launchers remain.
- Preserved original Monaco/xterm/font/icon bytes through manifest-based staging;
  fixed original webpack chunk/font/worker URLs without editing minified assets.
- Added a typed frontend adapter and persistent Monaco document store. Saves carry
  disk/edit revisions and SHA-256; dirty buffers survive refresh/theme/layout.
- Added original standard/bare/blink project templates and checked-header includes
  in generated sketch and optional C++ sources. Browser fixtures now match them.
- Ported pure serial decoding, concurrent preference merge and CPU/RAM/storage
  worker-budget decisions, comparing directly against original Python behavior.
- Verified native worker execution, C/C++ syntax and diff calculation. Same whole
  webview survives three native transfers with model, view and undo/redo history.
- Fitted the native frame to the monitor work area using logical sizing once.
- Fixed a real Windows ConPTY probe hang: cursor reply, concurrent final-frame
  draining, child-exit observation and bounded teardown. Unfinished OS cleanup
  retains an exclusive probe lease; arbitrary commands are not exposed.
- Preserved active buffers on failed project preflight, rejected slow stale reads,
  prevented project switch/refresh during save and cleared resolved disk conflicts.
- Added Ubuntu and Windows launch/verification scripts, owned Cargo target paths,
  Windows argument forwarding, strict checks and separate timed native CI probes.
- Added CI for Ubuntu 24.04 AMD64/ARM64 and Windows Server 2022, with exact-host
  limits. Superseded runs were cancelled; latest revision remains under validation.

## Code checkpoints

| Commit | Change |
| --- | --- |
| `44c43cf` | First fixture-only Rust/Tauri prototype and native CI |
| `9013b45` | Monitor work-area fitting and migration CI concurrency |
| `f0c2e0e` | Explicit UTF-8 for cross-platform differential subprocesses |
| `01b7b83` | ConPTY lifecycle, editor transitions, resource policy port and Windows scripts |
| `a1ae2fb` | BOM/EOL saves, bounded native ACKs, transport/lock proofs and downloader work limits |
| `b36864a` | Reject Windows mixed-separator parents before filesystem access; native/mixed escape probes |
| `7dbefb5` | Ownership policy, native process identity and atomic fixture registry transactions |

Audit, documentation and relevant skill updates were deferred until the final
code/check stage as requested. The tracked ledger and handoff record remaining
work; no migration completion or release replacement is claimed.

## Historical validation at `01b7b83`

At that earlier checkpoint, 35 Rust tests, 19 frontend tests, frontend build/typecheck,
strict Clippy, 10,633 reference-comparison cases, native PTY and 28 webview checks
passed on Ubuntu 26.04 AMD64. Ubuntu 24.04 ARM64 CI also passed at `01b7b83`.
See STATUS.md/ci-results.json for source-qualified hosted results. Physical USB,
Windows 10/11 desktop qualification and full application parity remain pending.

## 18 October 2026 — Checkpoint `a1ae2fb`

- Preserved BOM and exact unchanged/undo source bytes through actual Monaco;
  edited mixed newlines disclose normalization, and old registrations cannot
  accept stale save baselines.
- Added native window/stream/sequence/byte consumption acknowledgements, bounded
  frontend event queues, timeout/abort behavior and no late ACK after failure.
- Added isolated Rust/Python native-lock interoperation and four pure transport
  helpers retaining dynamic programmers, default baud and ESP limit 921600.
- Fixed the actual downloader DialogFit 60 ms Configure feedback loop without
  dropping work-area fitting, and hardened six native verifier assumptions.
- Corrected missing gnome-keyring in legacy CI prerequisites; restored the root
  launcher after explicit user confirmation of an external deletion.
- Local evidence at this historical checkpoint: 47 Rust/40 frontend/45 native
  checks, 23,672 differential cases plus 8 lock checks pass. Native reference suites and downloader 100/200% checks pass;
  exact latest Windows/Ubuntu CI results remain separately recorded.


## 19 October 2026 — Checkpoint `7dbefb5`

- Ported pure legacy PID-shaped ownership planning, including creation-time/PID
  reuse checks, conservative unknown peers, project/port collisions, explicit
  release and refusal of silent workspace replacement within one process.
- Added explicit native process-creation identity queries for Linux and Windows.
  Controlled native process times agree with psutil; permission, malformed,
  unavailable and raced observations remain conservative.
- Added fixture-only registration transactions against fixed settings/lock names:
  acquire the interoperable OS lock, freshly read JSON, gather evidence under the
  lock, preserve unrelated fields and records, skip unchanged writes, and stage,
  flush/sync, byte-recheck and atomically replace a granted plan. Committed
  staging names are disarmed from cleanup; failures never acknowledge a save.
- Proved simultaneous claims have one live winner, live controlled Python-shaped
  records reserve their ports/projects, release preserves concurrent Python
  updates, and a confirmed exited owner permits reclamation without pruning its
  record. These are disposable fixture transactions, not live coexistence.
- Local evidence: **80 Rust tests, 40 frontend tests, 46 native webview checks**,
  **23,906 differential inputs** (234 ownership inputs including seven typed
  refusals), **8 lock + 6 process + 9 registry checks**, strict lints/builds and
  native PTY pass on Ubuntu 26.04 AMD64.
- Hosted migration CI at `b36864a` passed on Ubuntu 24.04 AMD64/ARM64 and Windows
  Server 2022 with 46 native checks each. Legacy Desktop compatibility at `a1ae2fb`
  passed all 14 jobs; its separate Native UI layouts run failed. Latest `7dbefb5`
  hosted runs are pending and require their own evidence.
- Remaining gates include native file identities/TOCTOU/ACL/durable crash recovery,
  shared package-event roots, coordinated live ownership with one process per
  workspace, complete parity and native desktop/hardware qualification. Python
  remains the production release; no live project/settings writes are enabled.

Audit/Skills updates were again deferred until the final code/check stage.
The root launcher restoration and available private Python runtime were retained;
unrelated installer, catalog and bootstrap-settings changes were preserved.

## 19 October 2026 — JSON, focus and capture checkpoints

- `d68b50a` preserves opaque unknown JSON fragments, large integers and marker
  objects; exact float parsing retains Python process timestamps. No global
  arbitrary-precision or raw-value feature was introduced by the host.
- Fixed actual hidden-review-bar focus fallback without changing unrelated focus,
  models or history; verified wide/compact layouts through contained child panes.
- `4cf730b` confines optional captures to checkout temp or durable audit captures,
  including traversal/symlink rejection. Both scales pass all 12 editor checks.
- Fresh local result: 90 Rust, 40 frontend, 46 native webview, 23,909 differential
  inputs, 8 lock + 6 process + 9 registry checks; the registry has 21 Rust tests.
- At `4cf730b`, all five migration/editor jobs, all 14 compatibility jobs, all
  four native-layout jobs and native ARM64 passed. Exact links are in ci-results.json.
- Baseline checkout inventory is preserved. Ten intentional baseline amendments
  cover two production sources, seven verifiers and one legacy CI resource.
  No component is approved for Python retirement or live settings/project writes.

## 19 October 2026 — Requested Rust tools setup

- Read the shared Rust Plugins Comparison conversation and inspected its sources
  with two subagents before installation.
- Installed 18 selected Rust skills, Superpowers 6.4.2, official Context7 1.0.2,
  Rust Analyzer/rust-src 1.99.1 and the pinned experimental Rust LSP server.
- Verified an isolated native analyzer/symbol session and both Context7
  documentation operations. Enabled only six read-only LSP tools and retained
  all pre-existing Codex settings and project instructions.
- Preserved the interrupted file-identity changes without committing or promoting
  their incomplete integration checks. See tooling-setup.md and HANDOFF.md.
