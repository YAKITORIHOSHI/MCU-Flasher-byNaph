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
for the current commit. Revision 31e6e24 passed nine of ten jobs, including
Windows and supported Ubuntu runtime jobs.
That exact revision also passed the native Ubuntu 26.04.1 runtime fixture on
2026-10-11. Subsequent fixes require a successful background source scan,
complete source-model coverage and matching
renderer tokens before acknowledging Cloud Pull, and restore cloud prompt
mouse access and short-screen scrolling. The updated fixture passed 46 runtime/manifest checks
plus three real detach/reattach/reload cycles on GNOME XWayland and Wayland,
retaining active and inactive Unicode unsaved buffers. Verify these behaviors
again when changing recovery or Pull.
Run node direct/verify_editor_recovery.js and the real direct/verify_runtime.py
fixture. Never relax dirty-text assertions or clear snapshots to obtain a pass.

Ubuntu execution now requires 24.04 or newer; verify older release rejection only.
The old Xft color-font fallback and its obsolete CI diagnostics were removed.
Keep the shared Qt/Python ABI check for custom older Python on supported hosts.
Earlier successes do not prove the current revision.

Launch through MCU_Flasher or bash direct/ubuntu/run.sh on Ubuntu 24.04 or newer.
Diagnose with --check. During verification, mock installers and use isolated
fixtures under temp; never repair the live package store. Verify the actual desktop, including Wayland when
available, resize/scaling, editor dirty recovery, developer long-text wrapping,
cloud login, upload/open/push/pull, independent cloud/developer connectivity,
and the Ubuntu credential store. Offline Mode applies only to board/library
download and preparation. Cloud copies belong under the generated
_MCUFlasherByNaph_src/Cloud folder. Keep secrets out of source, logs and captures;
never fall back to plaintext credential storage.

The 2026-10-11 native audit passed 25 cloud UI, 18 developer UI and 26
performance checks, plus isolated service/connectivity/launch/Bootstrap/PTY
fixtures. All 16 responsive Qt/Tk checks also passed at 175% and 200% on an
isolated native Xvfb display with the assertions unchanged; GNOME auto-maximizes
oversized synthetic windows, so use Xvfb for artificial monitor coordinates.
Actual Wayland cloud prompt checks also passed strict frame/input containment
and native mouse routing. Shared fitting measures native decorations and lowers
minimum sizes only as required to keep the complete frame inside the work area.
For headless Cloud prompt checks, force overflow using measured content height
and frame clearance. Keep fixed monitor cases, complete field visibility and
text/font assertions; smaller offscreen decorations can fit a form without
scrolling at the same work-area height.
Terminal output scrollbars are hidden with viewport-scoped styles in both
renderers. Keep retained history, mouse-wheel and Shift+PageUp scrolling;
session-tab navigation stays visible when needed. Run
direct/verify_terminal_scroll.py against both actual offline pages with isolated
transports, no PTY or authenticated CLI. The tab-arrow fixture also exercises
native standard-icon assignments, palette contrast, real navigation clicks and
settled idle layouts. Ubuntu QWebEngine checks of the Windows HTML do not
establish native WebView2 behavior; keep the Windows terminal checks in CI.
The standalone browser-loading suite passed 29 checks on Python 3.14.4,
including the compatibility initialization, and the required runtime fixture
passed all 46 checks plus real renderer recovery.
A uniquely namespaced production-adapter GNOME Secret Service round
trip passed without reading existing account records. This machine's normal
private runtime is still unprepared and its launcher check reports missing
libsecret-tools. Temporary verification tools were checksum-verified and
extracted under temp/; no system installer or live metadata generation ran.
The user initially requested dependency cleanup, then clarified that the shared
application copy must retain its working Windows runtime. The current repair
target is Stable Release-4; Stable Release-3 is the unchanged Windows recovery
source. Windows `src/_python` and `env` were restored into Release-4 and all
31,149 copied files were SHA-256 verified. Do not remove these Windows resources
as part of Ubuntu cleanup. Windows uses its bundled Python and WSH launcher;
Ubuntu uses Bash Bootstrap and its separate `.venv-linux`. The Linux environment
was recreated for hardware-free verification, but the previously cleaned native
board packages remain unprepared. Let the Ubuntu launcher prepare those packages
when the user opens the application; verification must use isolated stores.
Windows launcher probes now ignore inherited Python overrides, and native CI
checks moved-environment repair using the actual bundled Python 3.14 installer.
The same-account receipt-authenticated XDG native PlatformIO downloads were
also explicitly cleaned, freeing another 5.1 GB outside the checkout. Internal
packaged aliases are unlinked only when they resolve within that architecture
tree. All 45 isolated maintenance checks passed, including external/cyclic-link
refusal and other-architecture preservation; the runtime CI includes them.

Run relevant hardware-free direct/ verifiers with .venv-linux/bin/python,
using isolated fixtures and temp/ captures. Preserve protected cache/journal
boundaries. Do not flash, reset hardware, replay terminal commands, or delete
accounts/sketches during verification. Fix reproducible issues, rerun affected
checks, commit and push validated changes, and report remaining on-device
checks. Do not shut down this Ubuntu machine unless I request it.
