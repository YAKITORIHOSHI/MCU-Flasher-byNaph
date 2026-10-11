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

Review the latest Desktop compatibility GitHub Actions results and artifacts.
Clean first-launch checks have passed on 22.04, 24.04 and 26.04. A broader
22.04/Python 3.10 downloader fixture has crashed in Tk Listbox.delete; inspect
any native Tk backtrace before changing production behavior. Report current
results accurately instead of treating earlier successes as proof of every
check.

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
