# Half-screen window alignment

The supplied five-second Ubuntu recording shows the main workspace extending
outside the desktop after left/right snapping. Images and video excerpts remain
in ignored task-owned audit storage; no user sketch content is published.

The application declared half the monitor width as a client-area minimum.
On native GNOME Wayland at DPR1, horizontal native margins total 22 logical
pixels. A requested 960-pixel complete tile therefore rejected the 938-pixel
client: the old minimum forced 960 client/982 frame. At an odd 1919-pixel
work area the corresponding frame was 981 against a 959-pixel budget. The same
probe on local xcb has zero horizontal borders and does not reproduce this
particular rejection; earlier xcb/frameless fixtures could not detect it.

The correction subtracts measured native horizontal frame margins from the
half-monitor frame budget. It takes the larger of QWidget's settled delta and
QWindow's native margins, which are available earlier on Wayland. All values
stay in Qt logical pixels. No fixed border width or extra scaling is applied.

A second regression reproduced after maximize -> monitor refresh -> restore:
maximized margins 0 reset the client minimum to 960; normal margins 22 then
returned without updating that minimum. ScreenWatcher now includes window-state
changes in its existing coalesced 60ms refresh. There is no new polling loop.

The decorated native fixture covers even/odd widths, repeated half-size and
normal restore, late post-show margins, maximize/refresh/restore, and initial
maximized show. It preserves text, cursor and content font. The existing Project
minimum/maximize fixture follows the complete-frame contract.

Qt documents the distinction between client and frame geometry and explains
that Wayland controls global positioning:
https://doc.qt.io/qt-6/application-windows.html
https://doc.qt.io/qt-6/qwidget.html
The checks establish accepted frame sizes and non-Wayland placement requests;
they do not automate GNOME/Mutter keyboard snapping, mixed-monitor hardware or
absolute Wayland placement. The application does not override the compositor.

## Local verification

Native Ubuntu 26.04 AMD64 / Qt6.12.0: four alignment cases at five fresh scales
(100/125/150/175/200%) on both native GNOME Wayland and Xwayland/xcb: 10 processes,
40 cases passed. Evidence: `temp/audit/window-alignment/native-bounded-final/`.
The tests cover accepted native geometry and application state, with finite
native configure-delivery deadlines; no abnormal exit or retry is accepted.

Full hardware-free runtime: 46 units and real Monaco/tool-tab/keyboard,
detach/reattach, dirty recovery and cloud Pull scenarios passed with normal exit
on native Wayland and offscreen Qt. Existing screen watcher checks: 3/3 passed;
Project minimum/resize/maximize check: 1/1 passed. Performance: 31 passed, one
native-only skip. Launcher verification before Fresh: 15 passed, seven native
Windows skips; release-source checks: 9/9 passed. Syntax, application guidance
alignment, seven LFS installer hashes and Git full integrity checks passed.

## Native hosted verification

Runtime source: `414b4d2ad3fb8707fc3004b65efbc6f7db712343`.
[Run 39978943884](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39978943884)
passes all four Windows/xcb jobs, 12 fixture processes per host (48 total).
Each host covers alignment and toolbar checks at five scales, plus full runtime
and responsive checks at 100%. The separate native Wayland jobs are pending
while installing display dependencies; results will be appended when known.

## Fresh preparation and manual compression

The user requested Fresh cleanup and manual zipping. Native Ubuntu Fresh ran
through the maintenance ownership/process guards using system Python, removing
13,436 generated entries (including the validated `.venv-linux`). A subsequent
preview found nothing further to clean. Personal settings, sources, logs,
protected caches/journals, shared stores and Windows resources were preserved.
The working Bootstrap setting remained unchanged.

A separate `dist/READY-TO-ZIP/PROJECT MCU Flasher by Naph - Stable Release/`
folder was prepared from verified committed defaults. All 18,171 regular files
were independently read and hashed after extraction; executable permissions were
restored. Its 17,496-file complete Windows runtime totals 1,265,081,823 bytes.
Bootstrap defaults are exactly `{"skip_updates": false}`. Personal settings,
notification/history files, development runtimes, Git, audit/preview trees and
protected project caches are absent. No app installation or live Bootstrap was
run to simulate first use.

The task-created ZIP used for preparation was removed after folder verification
in accordance with the manual-compression request. No new ZIP or GitHub release
asset was published. The pre-existing user's archives were not deleted by this
cleanup. Receipts: `temp/audit/window-alignment/manual-folder-{receipt,files}.json`,
`fresh-{preview,apply,after}.log`, `package-independent.json`.
