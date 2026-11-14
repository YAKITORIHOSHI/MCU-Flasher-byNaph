---
name: mcu-verify
description: "Run MCU Flasher hardware-free and isolated native CI checks, including Lite renderer/cancellation/visibility gates, keyboard routing, semantic navigation, pairing, save races, cross-device ticket deletion and installation; distinguish regressions from baseline failures and preserve Windows 11 verification limits."
---

# Verify MCU Flasher changes

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
  `BootstrapWindow.open` and `.adopt` so nothing reaches the real installation.
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
reveals and allows only the tab strip itself to change width. Half-height window
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
