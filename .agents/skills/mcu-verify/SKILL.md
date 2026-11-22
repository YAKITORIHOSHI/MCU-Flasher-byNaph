---
name: mcu-verify
description: "Verify picker action visibility through resize, sketch routing, ticket field alignment, settled native editor viewports, SMB release packaging and source saves, automatic Lite board targets, editable Auto-Save geometry, portable native CI fixtures, deleted-view cleanup, Bootstrap console encoding and shared main/Lite packages, conservative cleanup, progress replacement, Bootstrap routing, resources, sample viewing and short ticket dashboards. Run MCU Flasher hardware-free and isolated native CI checks, including Lite multi-window/port-sharing, modal dialog, renderer/cancellation/visibility gates, keyboard routing, semantic navigation, pairing, save races, cross-device ticket deletion and installation; distinguish regressions from baseline failures and preserve Windows 11 verification limits. Verify the quick Bootstrap board stage and first-compile board tools without downloads."
---

# Verify MCU Flasher changes

Fractional editor-sheet checks use fit_host to wait for both Qt and Chromium
viewport axes before publishing the backdrop dimensions. A width-only wait can
observe a transient Windows height at 200% scale. Preserve the exact sheet-bound
assertions and the full 100/125/150/175/200% matrix; do not hide the failure by
widening tolerances or dropping a scale.

Release archive fixtures use Git index modes on SMB hosts. Unsupported chmod on
the owned scratch ZIP must retain verified 0755/0644 ZIP entry modes; real access
errors must prevent publication. Keep complete archive/runtime/hash validation.

Ghost Sketch publication handles SMB/GVfs refusing rename over an existing empty
folder: only EEXIST/ENOTEMPTY permits the POSIX fallback, after rechecking the
original directory identity. rmdir must refuse newly added contents; preserve a
replaced destination and the temporary original. Verify ghost_sketch on the
share and in native owned fixtures.

File-access checks cover writable shares that reject chmod, complete UTF-8/CRLF
writes, retained originals on access/storage/replace failures and expected-byte
conflicts. Run editor_features/storage and AI reject/undo/redo with owned files
on the actual share when available. A successful read is not save evidence.
Lite worker checks seed an obsolete framework override and require the main
catalog default for both target events and build resolution. The retired
framework command cannot change it; keep Rust layout/Clippy/test checks.


Glass checks include deleting a page with a queued backdrop refresh and retaining
its Python wrapper without another page call. Platform checks exercise cp1252
heading fallback without swallowing I/O failures; Ubuntu Bootstrap checks route
both heading forms into real stage/status/progress widgets. Serial-reader AST
fixtures must provide port_owner_window, and a held port must never open a monitor.
Preserve all display-scale cases when allowing slower native renderer matrices.

BootstrapRestartChecks uses real child processes, real atexit slot cleanup and
literal spaced paths. Check native Windows venv relay adoption through the short
bootstrap-handoff workflow; mocked relay cases alone cannot prove Toolhelp layout
or actual parent PIDs. Keep missing-resume and unrelated-owner refusal cases.

## Native control and fixture contracts

- Auto-Save checks measure the native child editor against the longest delay and
  its text margins, then use an ordinary mouse click to type and save a new value
  in every theme. Assert empty spin-button hit areas; the `NoButtons` enum alone
  does not prove usable geometry.
- Animation fixtures explicitly enable effects and clear the reduced-motion
  environment only for that case. Independently exercise both Windows animation
  and environment opt-outs with real popups at rest, usable keyboard selection
  and no concealed controls. Do not change the user's animation policy.
- Installation copies compare destination permissions with the source's native
  mode. POSIX also proves source execute bits; Windows cannot represent them.
  Board-generation fixtures without libraries explicitly provide
  `_library_search_directories` returning an empty list. Keep recipe/hash and
  unchanged-write assertions, all hosts and all display scales.
- Cloud fixtures narrow only the viewport and require exact card widths, zero
  horizontal scrolling and a resting/disposed fit timer. Native selector checks
  repeat the requested size once after compact reflow, retaining exact final
  geometry and reporting the axis, minimum, hint and display scale on failure.

## Prepared resources and short Developer Access

- Lite tools/verify_shared_store.py covers one physical core, concurrent readers,
  active-window/build cleanup exclusion, exact duplicate retirement, unique and
  conflicting packages, cross-drive copies, internal link relocation, preserved
  settings/projects/recovery and one progress key per package. Main package_jobs
  also checks different product event roots and legacy markers. Never clean a
  live project cache as part of these fixtures. Block cleanup in a fixture while
  exercising status/Stop and require no success after cancellation. Rust startup
  tests retain the requested sketch through busy work and dispatch it once idle.
- Lite tools/verify_resources.py covers read-only main manifests/configured
  libraries, all sample source extensions, ZIP samples, opaque IDs, cancellation,
  exact target preservation and bounded publications. Keep main bytes unchanged.
- direct/verify_lite_launch.py records both host Bootstrap handoffs and a cold
  Ubuntu repair without launching setup or either real workspace. Normal Lite
  selects the prepared environment before the Windows base interpreter. Cover
  the headless preflight and reuse of an owned runtime with an older launcher.
- owner_tickets checks the filters' shared height, equal widths and gaps at
  683x730, the wide single row, painted ribbon insets and metric/ticket minima
  after scrolling at 380x420. Check actual captures as well as bounds.
- Lite previews include installed resources and light/dark/short sample code.
  Native viewer find offsets preserve Unicode and stale chunks cannot replace a
  current file. Real compiler qualification records session-local preparation
  results through worker.session, never the retired shared prepare folder.

## Lite windows and shared ports

- `tools/verify.py` starts two real workers on one state directory, checks the
  main app's port claims in Lite and Lite's in the main app (temporary registries
  with a live fixture process), busy peer locks, sketch ownership, package-lock
  Stop and the single portal. Clear `_pid_create_times_cache` before asserting a
  release: the backend caches process inventory for 2 seconds.
- Rust tests cover modal layers, Escape, simulated system folder results and
  visible picker actions while resizing the same window. History checks cover
  canonical source-file paths, occupied folders and validation before window
  dispatch. Never let a test reach `desktop::open_window`, where `current_exe`
  would point to the test binary.
- Run main port verifiers (projects, monitor_connection, ubuntu_ports,
  connection_loss, actions, upload_workers) after changing `main/core/config.py`.

## Lite screen, cancellation and native visibility gates

- Run locked cargo check/test and strict Clippy in LITE-MODE. The real egui
  widget matrix covers 100/125/150/175/200% logical layouts, stable streaming,
  persistent Stop, long source/card bounds, and small/short/portrait dialogs.
- Run Lite tools/verify.py, verify_preparation.py, verify_history_search.py and
  verify_developer.py with the private runtime. Cover silent/dripping child Stop,
  absolute deadlines, exited-parent descendants, unrelated sibling preservation,
  failed Job assignment, late-result suppression and exactly-once upload handoff.
- The helper fixture must install the borrowed Windows hide hook and check actual
  native visibility before Ready. Keep owned foreground grants and preserved
  maximization. Main owner_tickets checks actual workspace input while portal
  forms/notices are open and safe owner closure with queued services.
- tools/verify_layout.py saves actual Rust renderer previews on its own Xvfb
  display or isolated Windows Actions (--native) only. The real renderer runs at
  host DPI; simulated widget scaling and forced native Qt scales are separate
  evidence. Do not claim Windows 11, Wayland/mixed DPI or USB from those checks.
- Run main runtime, controls, owner_tickets, owner_ticket_service, shutdown,
  bootstrap_promotion and ubuntu_bootstrap_window after shared GUI changes.


Ticket deletion checks use two service instances with independent caches and a
mocked shared cloud: cover partial and final deletion, no resurrection, failed
delete acknowledgement and unsent creation retry. Portal checks must use the
dedicated CreateTicketDialog, asserting unchanged cards before success and on
cancel/failure, plus restoration after a failed deletion. Assistant loading
checks assert unchanged status text over time and retained slow-start help.

## Ubuntu Bootstrap window and update gates

- `verify_ubuntu_bootstrap_window.py` runs the real Qt setup view offscreen with
  the private runtime; `--render-dir temp/...` saves captures. It never runs setup.
- `verify_ubuntu_launcher.py` `BootstrapWindowChecks` use recorder processes for
  the window; every launcher fixture patches `owned_installation_elsewhere`,
  `BootstrapWindow.open`, `.adopt` and `open_first_run_window` so nothing reaches
  the real installation.
- `verify_target_packages.py` covers the quick board stage (`--auto`,
  `catalog_ready`, Offline Mode's complete certificate), first-compile stamps and
  the bridge's clean preparation child, with no downloads. A real end-to-end check
  belongs in an isolated HOME/XDG copy under the scratchpad (setup.py, then
  `target_packages.py` and a guarded `offline_platformio.py run` per board).
- `verify_app_update.py` `IncrementalSourceChecks` cover installed-file reuse,
  never-installed paths and the one-update lock with real Git and a local bare
  server; `verify_bootstrap_updates.py` and `verify_controls.py` check that no
  view asks before installing updates.

## Upload, restart, monitor, ticket and terminal gates

Run `direct/verify_upload_images.py` for nobuild flash images (real
PlatformIO `envdump` when espressif32 is prepared; never a build, port or
upload), `verify_monitor_connection`, `verify_serial_reader` (reset notices
precede boot output), `verify_upload_workers`, `verify_upload_logging`,
`verify_ubuntu_logging`, `verify_platforms`, `verify_reset_build_cache`,
`verify_owner_tickets` (in-place deletion, in-button spinner),
`verify_combo_popups` (concealed first frame, snapshot motion),
`verify_window_alignment` (Wayland tiling allowance), `verify_posix_terminal_state`,
`verify_terminal_startup` (exit disposal, engine events) and
`verify_terminal_scroll` (real-page links, clicks and clipboard keys) and
`verify_bootstrap_updates` (no `arduino-cli upgrade`). Physical boards and native
GNOME tiling remain separate evidence.

## Navigation, glass, motion and update gates

Run source_navigation, editor_features, editor_review_ui, library_samples,
combo_popups, controls, serial_view, svg_icons, app_update, user_install,
release_sources and monitor_matrix. Popup tests wait for `PopupMotion.running()`
to end before sampling resting pixels; the monitor matrix waits for compact tab
reveals and allows only the tab strip itself to change width. After each resize
or assistant toggle it waits, within a bound, until the workspace stops moving and
its fit timers are idle: a fixed delay was too short on the Intel macOS runner.
Windows CI at 200% clamps the project selector below its advisory minimum; only
the Cloud scroll viewport may yield there. Timer tests keep 150 ms of margin. Half-height window
requests must be refused (three quarters is the minimum).

## Editor and input improvement gates

Run native_input, cursor, source_navigation, editor_features, file_access, user_install,
owner_ticket_service, owner_tickets, library_samples, controls and svg_icons.
Include detached_actions, terminal_scroll and release_sources for backend action
ownership, completed native viewport fitting and committed installer identities.
Real semantic coverage requires libclang==18.1.1 in the isolated runtime; an
LLVM-unavailable skip does not qualify semantic resolution. Include the real
slow-parser deadline fixture on Windows venvs to prove descendant retirement. editor_features
loads the real offline Monaco assets for pairing/escaping/indentation/history,
read-only previews and acknowledged-save races. Installation checks mock all
runtime/package work and menu publication. No live install or account is used.
The POSIX cold-setup fixture embeds its exact owned sandbox home as a fallback
for later host verifiers that reuse the copy without its launch environment.
editor-improvements.yml runs these checks on Windows and Ubuntu. The explicit
verify_native_input.py --native gate creates only owned fixture windows and
injects keys on an isolated Actions runner, checking actual foreign-process
keyboard queues. Record the runner OS; Windows Server leaves Windows 11 pending.


Run from the project root with the private runtime:

```bash
.venv-linux/bin/python3 -B direct/run_verifiers.py                # everything
.venv-linux/bin/python3 -B direct/run_verifiers.py --baseline HEAD
.venv-linux/bin/python3 -B direct/run_verifiers.py incremental upload reset
```

The runner removes `DISPLAY` and `WAYLAND_DISPLAY`. When running one verifier directly
on a desktop, do the same with `env -u DISPLAY -u WAYLAND_DISPLAY QT_QPA_PLATFORM=offscreen`:
refusal and notice fixtures use deliberately wrong release data and would otherwise
open real dialogs on the user's screen.
`--native` is restricted to isolated GitHub Actions runners. It retains that
runner's native display when comparing a platform-specific failure with the
same baseline; it must never be used to open fixtures on a developer's desktop.

`--baseline <ref>` reruns each failure against a clean export of that ref and
reports only what the working tree newly broke. Always use it before calling a
failure pre-existing. Name fragments select verifiers by file name.

## Choosing checks

- Build, upload or reset changes: `incremental reset_build_cache board_build_cache upload`.
  Serial lifecycle/restart changes also require `monitor_connection serial_reader serial_send connection_loss`.
  The owned POSIX PTY checks HUPCL; mocked waveforms do not qualify physical USB-driver transients.
  `verify_incremental_builds.py` drives the real compile worker and PlatformIO.
  Mocked-compiler fixtures cannot see PlatformIO deleting objects.
- Bootstrap or package changes: `bootstrap offline package`.
- GUI changes: `runtime responsive controls performance shutdown package_browser monitor_matrix glass
  interface_size taskbar_identity renderer_color_depth`.
- Assistant changes: `assistant_startup ubuntu_ai_panel embedded_viewports`.
- Dropdowns: `combo_popups`, plus native `verify_ui_layout.py` at every scale.
  Include compact Actions/Options, alpha corner pixels, cached repaint and native
  Windows Settings open/close/heartbeat in `controls`. Sample source find and
  declaration navigation use `library_samples` in a separate PyQt5 process;
  `package_browser` covers the installed filter. Update prompt ownership and
  failed Skip preference persistence use `controls`, while mid-check cancellation
  and post-prompt revalidation use `bootstrap_updates` and source policy `app_update`.
  CMD/UNC changes: `cmd_directory` on native Windows and `terminal` for ConPTY.
  The SMB fixture owns its share and skips explicitly if the runner cannot create it.
- Host admission or setup: `ubuntu_release linux_distributions macos_adapter launcher_folder`.
  `direct/verify_ubuntu_first_run.py --execute` is the real cold-start proof on any POSIX
  host, and `other-platforms.yml` runs it on real Fedora, Mint and macOS.
- Editor changes: `editor` plus the JavaScript `direct/verify_editor_*.js` checks.

## Reading results

- A verifier can pass every test and still exit 139: PySide6 can crash during
  interpreter teardown. Read the unittest summary, not only the exit code.
- A timeout under parallel load is not a failure until it repeats alone. Rerun
  with `--jobs 1 <name>`.
- No verifier may open a port, install anything, touch live caches or open a real
  window. Windows, macOS, Fedora, Mint and ARM results need their own hosts; a pass
  here does not qualify them. `verify_terminal_startup` is Windows-only.
- `verify_ubuntu_launcher` needs the root launcher CI builds with
  `direct/ubuntu/build_launcher.sh`; remove it afterward.
