# Migration status

Updated: 19 October 2026, Asia/Manila. Code checkpoint:
`4cf730baf4db3399e16017bbb17abbb2419b126b` on `codex/rust-tauri-migration`.
[Current native CI](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39179835907).

**Actual migration code is implemented in `desktop/`, but full migration remains
unfinished. The Python application remains the release and behavioral reference.**
No Python application service has been retired and no live project/settings
writes have been enabled.

## Implemented scope

| Area | Working subset | Remaining boundary |
| --- | --- | --- |
| Contracts/project core | Owner-scoped root manifests, SHA-256/revisions, read/save acknowledgements, unchanged-write avoidance, original scaffolds and checked header includes | Fixture-only; native file identities/ACLs, races, shared writer registration, recovery and compatibility policy remain gated |
| Pure service ports | Serial text decoding, snapshot preference merge, resource-budget policy, four target transport helpers | No real serial, user-settings persistence, build or upload integration; full target validation remains in Python |
| Ownership policy | Legacy PID-shaped claim planning, creation-time identity/PID reuse, conservative uncertain peers, normalized port claims, explicit project/port release and collision results | One workspace per PID; no live registration or shared-PID multiwindow schema |
| Native process evidence | Explicit PID and own-process creation identity using Linux proc data or Windows process times; uncertain/denied/raced observations remain conservative | Process probes are implemented; this does not establish production process/session ownership |
| Locked ownership transaction | Fixed fixture settings/lock, fresh read and evidence collection under the interoperable OS lock, unchanged-write avoidance, atomic staged JSON replacement; opaque unrelated JSON fragments and retained timestamp spellings survive | Fixture-only; native identity/TOCTOU/ACL and durable crash recovery remain unresolved |
| Editor | Persistent Monaco models, stale-read/save rejection, clean/dirty manifest reconciliation, BOM-preserving saves and exact unchanged/undo bytes for mixed newlines | Search/syntax/recovery/AI and complete project workflow still pending; edited mixed newlines normalize with visible disclosure |
| Native terminal fixture | Fixed self-child PTY, Unicode/ANSI, ConPTY cursor reply and bounded cleanup; exact window/stream/sequence/byte acknowledgement after renderer consumption | General interactive shells, process-tree/session/device ownership, terminal input/paste/capability parity still pending |
| Native editor transfer | One whole webview transferred out/back three times, retaining model/view/history | Unstable API feasibility probe; editor-only detach UI and native keyboard/IME/focus/clipboard/dialog/monitor qualification pending |
| Resources/style | 22 original asset sources staged as 25 identical copies; offline worker/C++ chunk; three static glass palettes | Complete package provenance/notices remain a public-packaging gate |
| Existing editor fixes | Hiding an AI bar moves its focused descendant to Monaco or a visible toggle before applying hidden state; fallback does not steal unrelated focus. The verifier uses a contained child viewport sized in Qt logical units | Native NVIDIA 200% attempts remain incomplete; full input/graphics/monitor qualification is separate |
| Existing downloader fix | DialogFit stops repeated unchanged native geometry/minimum writes; changed work area/frame/client/minimum/scale still refits | Native Windows/full application performance qualification remains separate |

## Passing local evidence

Ubuntu 26.04 AMD64 at `4cf730b`:

- **90 Rust tests, 40 frontend tests and 46 native webview checks** pass, together
  with strict Clippy, typecheck/build and the real native PTY fixture.
- **23,909 differential case inputs** comprise serial 325, preference merge
  1,335, scaffolds 16, resource budgets 8,960, transport helpers 13,039 and ownership
  policy 234. The ownership inputs include seven intentional typed refusals;
  the transport probe separately records 51,993 equivalent comparisons and 163
  explicit type refusals. Neither probe proves full target/toolchain validation.
- **8 cross-language lock checks, 6 native process-identity checks and 9 native
  registry checks** pass. Registry fixtures exercise simultaneous competing
  claims, live native/Python-shaped ownership records, explicit release, crash
  reclamation without peer pruning and preservation of concurrent Python writes.
  The Rust registry subset now has **21 tests**, including opaque big numbers,
  marker-shaped objects, retained epoch timestamps and no-op/failed-write behavior.
  All JSON writes remain inside disposable audit fixtures.
- The latest 46-check local Tauri run used a checkout-local Xvfb and process-local
  Mesa settings. Xvfb was extracted from official Ubuntu packages into
  `.migration-tools/`; no system package installation or persistent graphics
  policy change occurred. This is layout/IPC fixture evidence, not GPU or
  performance qualification.

Legacy reference evidence:

- Runtime **46 tests** plus real Qt/Monaco tab, detach/recovery and cloud-buffer
  replacement smoke were freshly rerun with the private runtime and pass.
  Offscreen/Mesa execution emitted native-feature/Vulkan graphics diagnostics;
  no deprecation failure occurred.
- The editor suite passes **10/10 at 100%** on the native display and **10/10 at
  200% with verified Mesa OpenGL llvmpipe**. Focus/visible-toggle/no-toggle paths,
  unrelated toolbar focus, active/inactive buffers and actual undo/redo are
  checked. NVIDIA 200% attempts timed out or were stopped and remain incomplete.
  Qt still initializes a Vulkan context on NVIDIA during the passing Mesa run;
  this is not an entirely GPU-free or hardware-GPU performance result.
- Downloader appearance **17/17** and performance-work limits **7/7** pass at
  **100% and 200%**, with warnings as errors. No warnings were observed there.
- Custom/chooser suites at both scales each have **18 passes + one Windows-only
  skip**. Board search 25/25, PTY lifecycle 19/19 and two targeted Qt geometry
  checks at 100/175/200% pass. These are not the full responsive/Windows matrix.

See [verification.json](verification.json), [native report](native-webview.json),
individual JSON summaries and local `logs/`. Older logs retain superseded failures;
use each report's source commit and scope when interpreting it.

## Native CI and qualification

At `4cf730b`, all five migration jobs passed: Ubuntu 24.04 AMD64/ARM64 and
Windows Server 2022 native prototypes each passed 46 webview checks, and both
Windows editor binding ranges passed the 12-test suite at 100% and 200%.
The separate 14-job compatibility, four-job native-layout and native ARM64
workflows also passed at this exact revision. See the audit's `ci-results.json`
for job links and source-qualified results. Hosted runners do not qualify
Windows 10/11 desktops, physical Pi/USB, or the completed replacement.

The current [migration/editor workflow](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39179835907),
[compatibility workflow](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39179835912),
[native layouts](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39179835920)
and [native ARM64](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39179835884)
all completed successfully. The earlier timestamp, geometry/focus and audit capture
failures remain recorded in ci-results.json with their corrective commits.
No Windows 10/11 desktop, physical Pi 5 or USB session has been run for the replacement.

The capture guard adds two confinement checks to the ten editor DOM/model/focus
checks. The full **12/12 pass at both scales** locally on private Xvfb; the 200%
run emitted Chromium shared-image diagnostics after successful assertions. The
passing Windows jobs retain the same assertions. This does not establish
hardware GPU performance, full keyboard/IME behavior or completed Tauri parity.

## Preserved baseline and amendments

The structural inventory retains 300 source/launcher/verifier entries (280 Python,
141,796 lines), 123 Python verifiers and 133 resource hashes. It is not a complete
behavior/performance audit. [Reference amendments](reference-amendments.json)
separately record **10 unique amended files**: two production files
(`tk_glass.py` and editor `index.html`), seven legacy verifiers and one compatibility
CI file. **291/300 source entries and 131/133 resource entries** remain byte-identical.
The 22 staged asset sources remain unchanged across all 25 copies; do not rewrite baseline hashes to
hide changes. See [parity.md](parity.md) for capability-specific retirement gates.

## Still unfinished

Native file identities, final-replace/root-alias races, ACL/xattr preservation and
durable crash recovery; shared package-event/store roots; coordinated live
legacy/new ownership with one process per independent workspace; full
behavior/failure/performance baselines; settings import and rollback; complete
project selector/Modify/recovery/AI/search/syntax; dynamic targets/frameworks and
programmers; offline tools and exact-byte build caches; serial/compile/upload/reset;
downloader/package jobs; fixed-provider cloud/vault/conflict/Pull rollback;
setup/repair/maintenance/auxiliary UI; licenses and native release packaging.
Initial physical boards never narrow final feature coverage.

## Workspace boundaries

No live sketches, accounts, cloud records, serial devices, firmware, protected
caches or journals were used. Prototype writes stay in `audit/migration/scratch/`.
The unexpectedly deleted root launcher was restored from Git after explicit user
confirmation; no unrelated file was restored. Existing installer materializations,
catalog and bootstrap-settings changes remain unstaged. The private Python runtime
was available and used for isolated reference verification.

Read [HANDOFF.md](HANDOFF.md) for continuation commands and the next gate.
