# Scaled workspace controls — October 23, 2026

Toolbar implementation/original native matrix: `73cbfc3`.
Accepted-close correction/focused native follow-up: `02628dc`.

## Reproduction

A 1920×1080 display at 150% has about 1280×720 Qt logical pixels before taskbar
and frame reservations; a 1980-pixel width becomes about 1320 logical pixels.
The old Controls cutoff was 1350 and the primary cutoff was 1200. Consequently
the fixed cutoffs selected compact mode regardless of whether the content fit.
There was no second multiplication of Qt display scaling in this code.

The primary toolbar also measured hidden/state-dependent widget hints without
all native margins, gaps and spacer minima. A real QMainWindow's toolbar could
hide its entire right-hand widget action, including Project and Resources.
Resize history changed the result: 320 → 800 → 1280 could hide actions that were
visible in an initial 1280-pixel layout. Long project names exposed the same bug.
The extension did not establish a usable recovery route for these widget groups.

Previous verification placed the toolbar in an ordinary vertical layout and
mostly inspected buttons only if visible. It did not exercise native action
overflow. The new native-toolbar regression failed twelve assertions before the
production fix, including forced compact mode and hidden essential controls.

## Resulting behavior

Toolbar candidates now include their real font/style metrics, native margins,
spacing, mandatory actions and spacer minima. They select the fullest candidate
that fits the actual allocation. The optional project caption yields before
primary actions. Secondary actions, resource text and logo text reduce only when
needed. Project, Cloud and Resources stay directly reachable, including narrow
and restored layouts. Controls uses measured field/action/checkbox budgets too.

Font, project-label, style and theme changes trigger coalesced remeasurement.
Settled layout signatures prevent recurring idle work. An intermediate timer
loop was found during review and corrected before publication. Busy captions
survive icon-only and restored layouts; no installer, hardware or terminal
command is replayed by a resize.

The main window uses its children’s allocated logical widths and refreshes after
layout settles. Tool tabs follow their pane after splitter/sidebar changes, even
at an unchanged outer window width. Primary vertical chrome and workspace tab
padding are smaller; saved content fonts and usable navigation targets remain.

## Local evidence

Native Ubuntu 26.04 x86_64, private Python 3.14.4/PySide6 6.12.0. Qt toolbar and
workspace fixtures use the offscreen platform; native Tk checks use the desktop.
All fixtures isolate backend operations, cloud, preferences and hardware.

- New real QMainWindow toolbar fixture: five checks passed at 100%, 125%, 150%,
  175% and 200%. Fresh root execution at 150% also passed.
- Independent review: all three themes × 22 bidirectional widths, 320–1920
  logical pixels, including 1280/1320, long Unicode names, busy captions, popup
  transitions and zero recurring settled timer callbacks.
- Existing Controls checks: 38/38 passed. Full responsive checks at 150%:
  22/22 passed, including current root pane/navigation regressions and Tk checks.
- Runtime: 46/46 plus real simulated workspace renderer, keyboard navigation,
  source-tab state and repeated detach/reattach checks passed. Resource checks:
  31 passed, one native-only skip.
- Project selector four-page/three-theme sizing passed at 150%, including short
  and narrow dialogs. Downloader appearance: 17/17 passed at 150%; a stricter
  native Tk probe passed 18 Libraries/Boards/theme/opening/compact/restored
  combinations. No auxiliary production change was justified by these results.

The downloader's first focus check overlapped another native fixture and failed;
the isolated rerun and clean full suite passed. It is not treated as proof of a
production focus defect. Local Qt checks do not qualify native Windows rendering.

Root evidence: `temp/audit/responsive-fix/`; toolbar matrix/captures and exact
commands: `temp/audit/toolbar-validation.txt`; auxiliary measurements and captures:
`temp/audit/auxiliary-sizing-baseline/`.

## Accepted-close ownership

The existing close handler hid accepted windows but retained their owned
WebEngine views/pages. A new native renderer fixture failed before the correction
with retained `MCUMainWindow`, `QWebEngineView` and `QWebEnginePage` objects.
`WA_DeleteOnClose` now schedules destruction after the active event/callback
unwinds. Close vetoes remain alive, and other workspaces retain their own pages
and the shared WebEngine profile. No profile shutdown or recovery workaround was
added. The follow-up passes on both Windows binding ranges, including the older
Qt 6.11.2 host. These observations establish the ownership defect and passing
regression checks; they do not prove the exact third-party fault behind the
original access violation.

The new normal-exit verifier passed 18 local cases over three repetitions,
covering attached/detached/reattached editors, cloud/flashing vetoes, close before
startup and a surviving second workspace. A final last-window case uses Qt's
default automatic quit, with owned destruction observed before `app.exec()`
returns. It has no hard-exit bypass or accepted abnormal return code.
The full runtime verifier also passed 46 unit checks and real-renderer scenarios
with normal interpreter exit, plus an explicit owned-page destruction assertion.
Independent review repeated early close and second-workspace renderer isolation.

Final local responsive suite: 22/22 at 200% in 66.718s. Resource checks: 31 passed,
one native-only skip (32 total, 6.884s). Syntax, application-guidance alignment and
native workflow group selection passed. Git full integrity verification passed.
Normal-exit and red/green receipts:
`temp/audit/responsive-fix/shutdown-{red-native,green-native,runtime-normal}.log`.

## Native matrix and package

The [native UI run](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39962275329)
uses Windows AMD64 with both Qt binding ranges/Python 3.11 and 3.14, and native
Ubuntu 24.04/26.04 AMD64. Its `responsive` group runs fresh processes at all five
scales, including toolbar, workspace, setup, resources and Project fixtures.
The original matrix completed 120/122 fixture executions successfully. Ubuntu
24.04 passed 30/30 and Windows/Python 3.14 passed 31/31. Windows/Python 3.11
passed 30/31: its scale-1 runtime renderer assertions passed, then the process
access-violated at the existing `os._exit` teardown bypass. Its scale-2 runtime
passed. Ubuntu 26.04 passed 29/30: the scale-2 Boards fixture used the moving
glass backdrop's extent when calculating scroll positions. All 20 new toolbar
executions (five scales on each host) passed.

The Boards fixture now uses the embedded content window's bounds, matching the
production scrollregion. A controlled material-offset reproduction failed the
unchanged visibility assertion with the old calculation (327 > 321), then passed
with the correction. Native local 200% also passed. This is a verifier fix;
no Tk production layout was changed. Red/green receipts are under
`temp/audit/auxiliary-200-{red,green}.log`.

The [focused follow-up](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39972753019)
at `02628dce89a5cf2e01590ec9c66797f180e068cd` passed **46/46 fixture executions**:

| Native hosted system | Python / Qt | Result |
| --- | --- | --- |
| Windows Server AMD64 | 3.11 / 6.11.2 | 12/12 |
| Windows Server AMD64 | 3.14 / 6.12.0 | 12/12 |
| Ubuntu 24.04 AMD64 | 3.12 / 6.12.0 | 11/11 |
| Ubuntu 26.04 AMD64 | 3.14 / 6.12.0 | 11/11 |

The `edgecases` group retains full responsive and runtime checks at 100/200%, all
five toolbar scales and native Windows terminal checks. Normal-exit fixtures ran
18 close scenarios plus last-window automatic exit at each of those two scales
on every host (152 close cases total). Both Windows runtime checks also passed
their explicit owned-page destruction assertion before the existing hard-exit
option. No abnormal exit code, silent retry or softened visibility assertion was
accepted. The previously failing Ubuntu 26.04 Boards check passed in the full
responsive suite at 200%. Default `all`, `workspace` and `responsive` groups now
also retain normal-exit coverage.

Native ARM graphical desktops,
physical Windows 10/11 and monitor-to-monitor hardware transitions remain
separate qualification; macOS/Fedora/Mint entries are availability notices.

The combined source archive includes this correction and the Bootstrap changes.
It is built from committed fresh defaults with the complete private Windows
runtime and hydrated installers. See `dist/public/INTEGRITY.md` for the final
archive name, source identity and checksum. A local build does not replace a
published GitHub release asset.

Final archive: `MCU-Flasher_v11-responsive-bootstrap-final.zip`, 580,487,510
bytes / 18,170 individually verified entries, source `02628dc`.
SHA-256: `ff714df244e6e12d7fed08fbcaad36f16e3221c3326357279254714bdb476b9d`.
Independent ZIP CRC, changed-source/commit equality, fresh defaults and complete
17,496-file Windows runtime checks passed. Original user archive preservation and
owned intermediate removal receipts remain under `temp/audit/responsive-fix/`.
