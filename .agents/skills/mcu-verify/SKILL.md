---
name: mcu-verify
description: "Run MCU Flasher's hardware-free verifiers and separate new failures from pre-existing ones. Use after app changes, before a commit or release, or when a direct/verify_*.py check fails and you need to know whether your change caused it."
---

# Verify MCU Flasher changes

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
