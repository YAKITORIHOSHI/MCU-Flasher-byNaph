---
name: mcu-verify
description: "Run MCU Flasher hardware-free and isolated native CI checks, including keyboard routing, semantic navigation, pairing, save races and installation; distinguish regressions from baseline failures and preserve Windows 11 verification limits."
---

# Verify MCU Flasher changes

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
