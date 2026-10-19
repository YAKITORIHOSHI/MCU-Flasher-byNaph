# MCU Flasher migration: code reuse and behavior inventory

Date: 18 October 2026; checkpoint updated 19 October 2026
Status: Reuse inventory with implementation checkpoint `4cf730b`, supplemented by the ongoing
[implementation audit](../audit/migration/STATUS.md).

## Audit evidence and limits

The initial structural analysis parsed 277 Python files across `main/`, `src/modules/`, and `direct/` (133,282 source lines, including verification scripts). It inventoried imports, top-level functions, classes, and host dependencies without importing or running application modules. Focused reads covered the central bridge, editor and terminal host integrations, project scaffolding, target validation, serial delivery, build receipts, offline guards, package jobs, cloud operations, credentials, native launchers, CI, and architecture documentation. `src/syntax_checker.py` was inspected separately.

A checksum manifest records 131 resource/source entries: nine editor files, 99 assets, 19 reset-template/document files, the default preparation plan, two CI workflows, and the syntax checker. Local audit outputs are `temp/audit/rust-transition/source-map.json` and `reusable-resource-manifest.json`.

The durable inventory now covers 300 source/launcher/verifier files (280 Python
files, 141,796 lines), 123 legacy verifiers and 133 hashed resources in
[`source-inventory.json`](../audit/migration/source-inventory.json). Its
[parity ledger](../audit/migration/parity.md) records partial ports and retirement
gates. The earlier `temp/` reports above describe the planning session only.

These are inventory artifacts, not proof of full behavioral equivalence or an
exhaustive line review. Call-path/failure coverage and measured performance
baselines remain incomplete. New fixture and native prototype results are listed
separately in the durable audit; existing legacy results do not count as Tauri
results. The private Python runtime is available again and supplies isolated
reference checks; it does not become a Python application backend in the new
host. Native process/registration verification requires `psutil>=5.9,<8` in that
reference environment. Migration build tools use an ignored checkout-local directory.

The current local code checkpoint passes 90 Rust tests, 40 frontend tests,
46 native webview checks and 23,909 differential cases. Eight native OS-lock,
six process-identity and nine competing-registration checks are separate proofs.
The preference port contributes 1,335 cases and the registry has 21 Rust tests.
At `4cf730b`, all five migration jobs passed: Ubuntu 24.04 AMD64/ARM64 and Windows Server 2022 native prototypes each passed 46 webview checks, and both Windows original-editor binding jobs passed 12 tests at 100% and 200%. The separate 14-job compatibility, four-job native-layout and native ARM64 workflows also passed at this exact revision. See [CI evidence](../audit/migration/ci-results.json). Hosted results do not qualify Windows 10/11 desktops or physical Pi/USB behavior.

The current local prototype uses extracted private Xvfb/Mesa tools without system
package installation. Separate legacy editor review/child-viewport fixtures pass
10/10 at 100% and 200%, retaining models, dirty buffers and focus. The 200% run
verified Mesa OpenGL llvmpipe, while Vulkan startup was still observed; NVIDIA
hardware rendering there remains incomplete. Functional success is not a GPU-free
claim or a performance baseline.

## Disposition rules

1. **Reuse unchanged** when the asset, configuration, or tool already works across target hosts and licensing permits redistribution.
2. **Retain with an adapter** when behavior/content can survive but the Qt/Tk/Python host transport cannot.
3. **Preserve in the tool runtime** when Python code integrates directly with PlatformIO/framework internals or its package guards.
4. **Port proven behavior** when application ownership is moving to Rust; preserve data contracts, failure semantics, limits, and regression fixtures.
5. **Replace a toolkit surface** only because Qt/Tk widgets cannot run as the Tauri frontend. Preserve its workflow and visual tokens; replacement is not permission for a feature redesign.

Every component ledger entry must record original path/symbol, dependencies, schema or operation contract, host differences, reuse decision, rationale, reference tests, and retirement gate. A new library is not automatically a reason to replace proven behavior.

## Resource and service map

| Area and current sources | Reuse decision | Behavior and retirement gate |
| --- | --- | --- |
| Monaco bundle/workers/font: `src/editor/bundle.js`, `18.bundle.js`, `editor.worker.js`, bundled font | Reuse pinned asset versions initially. | Preserve offline loading and worker functionality. Upgrade Monaco separately after the host transition is stable. |
| Editor host: `src/editor/index.html`, `project_files.js`, `main/qt/editor_panel.py` | Retain HTML/CSS/JS/model behavior; introduce a narrow host adapter for Qt-specific calls. | Preserve actual save acknowledgement, dirty buffers, tab ordering, revision-aware reload and source manifests, recovery, completion, and shortcuts. Use `verify_editor_recovery.js`, `verify_cloud_editor_manifest.py`, `verify_project_file_lifecycle.py`, and related layout checks as reference contracts. |
| `src/editor/qwebchannel.js` | Retain for the legacy release; retire from the new host when no callers remain. | Replacing the transport must not change completion/error semantics. Do not carry Qt-only bridge code into the final Tauri runtime unnecessarily. |
| xterm assets, `src/editor/terminal.html`, `opencode.html`, `main/qt/terminal_panel.py`, `posix_terminal_panel.py`, `posix_ai_panel.py` | Reuse renderer assets and configuration; replace host transport and session ownership incrementally. | Preserve byte consumption acknowledgements, Unicode, capability replies, bracketed paste, hidden scrollbars, fit behavior, clear-as-display-only, and no command replay. Reference `verify_terminal_scroll.py`, `verify_terminal.py`, `verify_posix_terminal_state.py`, and Ubuntu AI/PTY checks. |
| Icons, fonts and palette: `src/assets/`, `main/core/theme.py`, `src/modules/ui_palette.py` | Reuse assets and theme values; map to CSS variables. | Preserve three palettes, legible text, selection/focus states, chosen fonts, and keyboard access. Inventory third-party notices before redistribution. |
| Workspace panels and auxiliary screens: `main/qt/`, `src/modules/arduino_lib_req.py`, Tk glass modules | Recreate toolkit surfaces only; retain workflow and proven layout/performance behavior. | Preserve project selection disposal, compact controls, downloader tabs/installed extraction, auxiliary windows, bounded list updates, and scale handling. Reference runtime, responsive, downloader appearance/performance, project, and Controls verifiers. |
| Central backend: `main/web_bridge.py`, `main/qt/signals.py` | Extract service contracts, then port services independently. | This is a 9,616-line orchestration module with Qt, serial, PlatformIO Python, and host imports. A file-by-file direct rewrite would conceal coupling. Trace each action's state changes, worker paths, emitted payloads, and failures first. |
| Project creation and files: `main/core/project_scaffold.py`, `file_utils.py`, bridge file operations, Modify dialog | Port application ownership with reference outputs; retain generated source text and rules. | Create-only project ownership, quoted header includes in generated `.ino`/`.cpp`, root file enumeration, literal paths, no unnecessary equal-content writes, and targeted source refresh. Preserve journals and do not reset unrelated sessions. |
| Configuration and workspace ownership: `main/core/config.py`, `config_store.py`, `main/qt/project_activation.py` | Retain schemas where compatible; port merge/locking/activation semantics. | Preserve cross-window snapshot merging, project/port ownership, failed-save reporting, and recent-project behavior. Reject conflicting legacy/new writers if locks cannot interoperate. |
| Build identity and cache: `main/core/compiled_cache.py`, `target_profile.py`, `arduino_inputs.py`, bridge build routines | Port receipt/target validation; retain board definitions, supported options, and canonical ownership boundaries. | Exact host/board/framework/transport, actual source/package/firmware bytes, interrupted builds, A→B→A reuse, and unknown-target refusal. Do not reuse a receipt merely because timestamps match. Reference `verify_board_build_cache.py`, target/board verifiers and `verify_storage_io.py`. |
| Build/tool execution: `main/core/toolchain.py`, `main/platforms/windows.py`, `ubuntu.py`, Arduino backend and native helpers | Preserve installed tools and CLI plans; port process supervision with host adapters. | Keep Windows short-path/UNC fixes, native Ubuntu environments, explicit Arduino CLI routing, output draining, single attempts, cancellation, and no implicit package installation. The app imports PlatformIO internals in places; those operations need a retained tool worker or equivalent certified CLI path. |
| PlatformIO compatibility/guards: `src/modules/offline_runtime.py`, `offline_platformio.py`, `windows_tool_paths.py`, `mbed_compat.py`, `zephyr_compat.py` | Preserve tool-integrated Python initially and, where necessary, permanently inside the private tool runtime. | These alter Python package/builder behavior; a Rust orchestrator alone cannot reproduce them. Preserve installed-only child behavior and tested framework/path adaptations. Reference bootstrap archive/process/builder, Mbed, offline, and Windows path checks. This is external-tool support, not a retained Python GUI/backend. |
| Serial pipeline: `main/core/serial_stream.py`, `serial_send.py`, bridge port/reader ownership | Port algorithms and state machine; choose Rust serial implementation against these contracts. | Preserve chunk-boundary decoding, generation identity, queue bounds, line endings, compile-time monitoring, upload reservation, disconnect handling, and no retries of partial writes. Current send limits are 64 KiB/message, 256 KiB queued, 16 messages; change only with evidence. Reference serial reader/send/delivery/view verifiers. |
| Package jobs/downloads: `src/modules/package_jobs.py`, `browser_loading.py`, `downloader.py`, `board_preparation.py`, bootstrap workers | Reuse worker contracts temporarily; port services in bounded steps. | Preserve atomic job records, store leases, queued/cancelled/interrupted states, checksum/archive containment, previous-payload recovery, latest-search semantics, and explicit preparation. Avoid two incompatible lock managers mutating one store. |
| Cloud: `main/core/firebase_provider.py`, `cloud_sketch_service.py`, `credential_store.py`, cloud UI | Retain deployment/schema/REST behavior; port service logic and secure storage. | Preserve revision conflicts, account separation, existing links, guarded local replacement, rollback retention, credential policy, and complete editor refresh after Pull. Reference cloud service/configuration/UI, Ubuntu cloud workflow, and rules checks. Never substitute a new provider or claim binary obfuscation authorizes database access. |
| AI editing/context: `main/core/ai_review.py`, `src/modules/ai_prompt_context.py`, recovery helpers | Preserve journal format, source manifest/revision semantics and OpenCode behavior; port application coordination. | New/deleted sources must reach AI context without resetting terminal/assistant sessions. Existing recovery copies remain protected. Use fixtures rather than live journals. |
| Syntax and compatibility analysis: `src/syntax_checker.py`, `main/core/board_catalog.py`, `board_compat.py` | Preserve observable output and limits; port incrementally. | Preserve content-bound diagnostics/caches, stale-result rejection, exact-target distinctions, search/ranking, and readable errors. An LSP redesign is separate scope and cannot silently replace existing diagnostics. |
| CPU/RAM/storage budgets: `main/core/build_resources.py`, `storage_resources.py`, `src/modules/runtime_resources.py` | Port policies with the same fixtures before tuning. | Preserve minimum CPU admission, four/six-core headroom, RAM and storage caps, worker-only discovery, and bounded logs/history. Measure whole process-tree cost, including the webview and tool workers. |
| Reset templates: `soft_reset/` | Reuse firmware/template inputs with exact-target validation. | Template availability is not proof of support on every host. Preserve existing recovery preparation/order and failed-write behavior; never exercise reset against user hardware during ordinary automated checks. |
| Setup/repair/distribution: `direct/windows/`, `direct/ubuntu/`, bootstrap, cleaner utilities, LFS installers | Retain validated tool payloads, checksums, setup knowledge and ownership rules; replace application launch plumbing as required. | Preserve native CPU/OS checks, standard-user app execution, targeted elevation, private tools, interrupted preparation recovery, and safe maintenance. Release artifacts must contain required runtimes, not just a small Rust executable. |
| Existing verifiers and `.github/workflows/` | Reuse as executable specifications and legacy comparison harnesses; add equivalents for new layers. | Keep native build/UI/runtime/hardware evidence separate. Preserve Ubuntu AMD64/ARM64 and Windows coverage; no passing test count substitutes for missing features or hardware tests. |

## Findings that affect the architecture

- The current project already has host abstraction (`main/core/toolchain.py` → `main/platforms/`). Reuse that division rather than inventing a Windows-only execution path and retrofitting Linux later.
- The editor is not an isolated HTML file. Its Qt bridge supplies save, source manifests, dirty state, recovery, syntax and action dispatch. Preserve these through an adapter contract before changing the UI.
- Terminal throughput already depends on acknowledgement after xterm consumes output. Preserve this application-level backpressure when changing IPC.
- The offline policy prevents dependency installation during workspace builds while allowing Firebase networking and user-owned terminal activity. Do not replace it with a global network-off switch.
- Exact targets and programmer transports already have explicit rules. Keep these rules; the four available boards are initial tests, not the product's target catalog.
- Framework/tool compatibility includes Python-level PlatformIO adapters. Keeping only the `pio` executable while discarding these hooks can regress supported builders.
- The original PID-keyed ownership schema represents one workspace per process.
  Pure Rust claim plans now retain its liveness and project/port collision rules,
  while native PID creation-time queries avoid a system-wide process scan.
  Disposable registration transactions share Python's OS lock and preserve
  concurrent updates. These fixtures are not production ownership: source-save
  leases, activation rollback, native file identity and package-store/event-root
  agreement still require coordinated implementations. Unknown native evidence,
  including a present zombie/dead PID, must continue reserving peer claims.
  Registry persistence retains private validated JSON fragments instead of
  converting unknown metadata into generic Value: large Python integers and
  nested private-marker-shaped objects survive claim/release. Known timestamps
  use `float_roundtrip`. The host's internal fragments do not require a direct
  `raw_value` feature declaration. Tauri's transitive feature means this opaque
  boundary remains necessary even without a direct host feature declaration.
- Toolkit-specific performance fixes express portable requirements: stable state must not trigger full-page repaint, hidden content must not repeatedly refresh, and large catalogs must remain bounded.
- Some behavior cannot be preserved by copying assets alone, especially detach/reattach of live Monaco state. This is a required prototype decision, not justification to drop the feature.

## Before a component can replace its Python counterpart

1. Trace original success, cancellation, contention and failure paths; record user-visible output and state changes.
2. Reuse compatible inputs/assets directly and document the reason for every necessary port.
3. Capture sanitized fixtures from the implementation contract, not from live personal data or protected caches.
4. Compare original and replacement on those fixtures. Review intentional differences explicitly; preserve corrected behavior rather than reproducing a known defect.
5. Exercise native Windows and Linux integration where host behavior matters; label unavailable architecture/hardware tests pending.
6. Demonstrate rollback/coexistence and resource limits before removing the legacy path.
7. Remove a temporary Python application adapter only when the parity ledger proves it is redundant. Preserve Python code that belongs to the approved external-tool runtime.

The initial audit changed only planning documents and local reports. Subsequent
authorized implementation adds `desktop/`, migration CI and durable audit files.
Two production surfaces now have narrow fixes: Tk `DialogFit` stops unchanged
geometry feedback, and the legacy editor moves focus before hiding the AI bar.
Seven legacy verifiers and CI dependencies have corresponding amendments recorded
separately with hashes in `reference-amendments.json`; baseline hashes are not
replaced. All 22 staged asset sources, reset templates, live settings, credentials,
tool stores and protected caches remain unchanged by this work.
