Copy the prompt below into Codex after opening this project on Ubuntu.

---

Continue MCU Flasher by Naph's Ubuntu validation on this machine. I use Ubuntu
24 LTS and 26 LTS. Read AGENTS.md and the mcu-flash-gui-dev skill; use xhigh for
every sub-agent. Inspect Git status and update from origin/main without
overwriting local changes, user sketches, settings, or protected caches.

The Windows work fixed copied PlatformIO Python overrides, native XDG paths,
PTY cleanup, compact cloud/developer layouts, and short downloader panes.
Native probes reproduced a Linux PySide6 6.12 reference-count defect on Python
3.11. Ubuntu Python 3.10/3.11 now uses PySide6 below 6.12; newer Python keeps
the current range. Readiness and the direct GUI entry reject the affected
combination before native widgets load. Do not remove this guard without
passing direct/verify_qt_binding_lifetime.py on older Python.

Review the latest Desktop compatibility GitHub Actions results and artifacts
for the current commit. Clean first-launch checks passed on 22.04, 24.04 and
26.04. Broader suites passed on Windows, 24.04 and 26.04, but a later 26.04
repeat exposed intermittent loss of an unsaved editor buffer during renderer
reload. A shared fix removes duplicate startup loads and requires matching
renderer tokens and restored text before acknowledging recovery; verify it
natively, including detach/reattach, repeated renderer reload and cloud Pull.
Run node direct/verify_editor_recovery.js and the real direct/verify_runtime.py
fixture. Never relax dirty-text assertions or clear snapshots to obtain a pass.

The 22.04/Python 3.10 browser fixture crashed in Tk Listbox.delete. Its fixture
now uses direct/tk_downloader_fixture.py without Qt imports, matching the
production downloader's separate process. Check the latest native result and
backtrace if this still fails; do not change production based only on a mixed
test-process crash. Ubuntu investigation was stopped at my request so it can
continue on this machine. Earlier successes do not prove the current revision.

Launch through MCU_Flasher or bash direct/ubuntu/run.sh. Diagnose with --check
and repair only if needed. Verify the actual desktop, including Wayland when
available, resize/scaling, editor dirty recovery, developer long-text wrapping,
cloud login, upload/open/push/pull, independent cloud/developer connectivity,
and the Ubuntu credential store. Offline Mode applies only to board/library
download and preparation. Cloud copies belong under the generated
_MCUFlasherByNaph_src/Cloud folder. Keep secrets out of source, logs and captures;
never fall back to plaintext credential storage.

Run relevant hardware-free direct/ verifiers with .venv-linux/bin/python,
using isolated fixtures and temp/ captures. Preserve protected cache/journal
boundaries. Do not flash, reset hardware, replay terminal commands, or delete
accounts/sketches during verification. Fix reproducible issues, rerun affected
checks, commit and push validated changes, and report remaining on-device
checks. Do not shut down this Ubuntu machine unless I request it.
