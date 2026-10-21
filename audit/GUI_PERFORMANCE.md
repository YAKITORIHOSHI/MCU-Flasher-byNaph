# Board/Library responsiveness and retained sleep state

Updated 22 October 2026, Asia/Manila. Scope: the current Python/Tk package browser and
Python/Qt Controls, alongside the unfinished native Rust migration. The user
reports lag in opening, searching, scrolling/resizing and selection, authorizes
backend changes, requests removal of Quit and asks to preserve lists in RAM.

The shipping GUI changes have passed isolated native Windows AMD64 and Ubuntu
24/26 AMD64/ARM64 browser/editor checks. Intel/Apple Silicon macOS editor
components also pass; full macOS application support is still pending.
The final focused Windows run is
[39653405199](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39653405199)
at `91632a1`. Exact earlier rendering checkpoints and retained failures follow.

## Changes

- Closing hides the loaded browser. Reopening reuses its widgets, catalogs,
  search, selected item/release and scroll state without catalog reload or an
  unconditional installed-files scan. Explicit refresh, folder/package changes
  and entering Installed remain refresh points. Internal final-workspace exit
  still closes the manager; active downloads/scans are not cancelled by hiding.
- Hidden progress/detail animations stop. Linux uses native Tk socket readiness
  for wake/quit, with no idle control timer. Partial messages alone retain the
  existing bounded deadline handling; unavailable native handlers fall back to
  polling. Windows retains its HWND/trigger protocol and hidden one-second poll.
- Content equality is checked on the catalog worker before publishing results.
  Equal cached/fresh catalogs reuse the same objects, including when both are
  queued before Tk handles either. Unchanged searches/inventories preserve
  visible lists rather than replacing rows or restarting detail scans.
- Large board-index JSON was parsed twice during one catalog load. A bounded
  schema-specific signature summary skips the redundant refresh-decision parse.
  Large raw JSON remains outside the RAM cache, application writes revoke the
  summary, and changed/missing/corrupt/schema-mismatched sources revalidate.
  This optional UI cache does not grant compiler/package readiness authority.
- One worker per Tk root prepares exact existing glass PNG bytes. At most 16
  current surface requests and one completed PNG of at most 8 MiB are retained.
  Work coalesces by surface, checks cancellation and yields between row batches.
  All native image/widget work stays on Tk; previous surfaces remain visible.
  Changed size/theme, hide/destroy and return-to-existing-size cases retire
  obsolete work and release unconsumed bytes. Idle workers/delivery timers stop.
- Button material refresh uses current native font metrics without cloning and
  deleting a font for each refresh; named font changes still update geometry.
- Constrained hosts display static elided Controls text with complete contextual
  tooltips. Other hosts pause marquees while inactive, minimized, disabled or
  hidden. Signal-blocked port updates and refused-selection rollback refresh
  the full text and animation policy explicitly.

## Local measurements

Isolated disposable Tk fixture on Ubuntu 26.04.1 AMD64; these are individual
local runs, not physical low-end-device certification or a sustained FPS claim.

| Operation | Before maximum UI dispatch | After maximum UI dispatch |
| --- | --- | --- |
| First map | 192 ms | 89 ms |
| Resize to 900 × 640 | 190 ms | 89 ms |
| Resize to 1200 × 800 | 295 ms | 96 ms |
| Warm reopen | 155 ms | 97 ms |

The final material may settle later while input is served between batches.
Native full repaint, first detail mapping and initial widget construction still
have measurable cost; this change does not claim all UI turns are below 16 ms.
Actual measured runs and raw instrumentation remain in
`temp/benchmarks/browser-render-audit/native-fixture-{before,after}.json`.
Pure uncached material generation previously consumed about 144–191 ms across
four UI-thread surfaces during first map/resize. Exact PNG byte comparisons
confirm that moving encoding does not change the material artwork.

Two overflowing Qt Controls previously produced 116 timer callbacks / 117 paints
in a 2.30-second background-window fixture. The revised background phase records
zero timer callbacks, with two initial paints. This is an offscreen functional
measurement; physical desktop timing remains separate.

## Verification and limits

All checks use disposable catalogs/files/socket endpoints and mock live
persistence, installation, networking and hardware. Captures/logs belong under
`temp/audit/gui-performance/`; CI retains `temp/audit/browser-performance/`.
Source review covers sleep/inventory/IPC, renderer lifetime/bounds and Qt policy;
review fixes include signal-blocked tooltip updates, minimized timers and unused
ready-PNG retention. Local current-source results:

- Browser loading/search/cache/sleep: **52 checks pass**, including actual Tk
  hide/wake preservation and native socket readiness.
- Bounded material rendering: **17 checks pass at both 100% and 200%**.
- Qt resource/Controls performance: **32 checks pass**, including the existing
  offline editor protocol and stale-parent event-filter recovery.
- Linux process ownership/wake/shutdown: **19 checks pass**.
- Private runtime: **46 checks pass**, plus isolated editor detach/recovery.

The full local appearance run initially recorded two focus failures while X11
focus moved to the Codex app. Current, forced-synchronous and prior source all
showed the same external takeover; the focus keeper correctly refused to steal
focus. A serial current-source rerun of both affected checks passes. Native
appearance qualification continues in isolated Xvfb CI; no focus behavior was
weakened. The first responsive capture run used Qt offscreen capture for a Tk
window and failed two image saves; its 135 layout cases passed. The verifier now
uses the existing native Tk capture helper, and its fresh full result follows
in the continuation evidence. Raw failures remain retained alongside reruns.

The focused [browser workflow](../.github/workflows/browser-performance.yml)
checks two Windows Python/Qt binding ranges plus Ubuntu AMD64/ARM64 without
repeating the historical full board campaign or running application installers.
Windows 10/11 desktop/device qualification, physical low-end-device performance
and the [expanded replacement platforms](migration/PLATFORM_SUPPORT.md) remain
separate requirements. No board upload/reset or production launcher switch is
part of this optimization.

## Editor, terminal and launcher continuation

The offline editor adapts the official Arduino IDE Light/Dark palette pinned
in [palette provenance](../src/editor/ARDUINO_PALETTE.md). Known C/C++ types,
constants and lexical call names receive the corresponding syntax colors;
existing comment/string/raw-string grammar states remain intact. Light hues
and dark comments have explicit small-text contrast adjustments. Solarized
retains its reading background with the compatible Dark syntax adaptation.
Font family/size, models, dirty text, tab order, selection and undo remain owned
by the existing editor; switching themes does not reload the project.

Terminal HTML/body/host surfaces now use the configured background/foreground.
OpenCode's padding moves inside xterm so FitAddon excludes it from grid space;
this fixes the extra bottom row clipping. The Monaco page fallback color now
matches its actual reading surface during startup and native repaint.
The real offline pages pass 40 retained-state/geometry/theme combinations at
each native Linux scale 100/125/150/175/200%, plus offscreen 75%. Tests cover
actual compact ~420px logical viewports, Qt/Chromium DPR agreement, hide-resize-
show, retained output/selection, no session restart and no typed/replayed input.
The first strengthened 175% fixture rejected Qt's legitimate 419px rounding of
a 420px request; a one-logical-pixel tolerance passes. Both receipts remain under
`temp/audit/embedded-viewports/`. Windows HTML tested on Linux is page evidence,
not native Windows execution. Expanded CI supplies separate host evidence.

Linux PTYs now use read/write readiness watches instead of a recurring idle
25/40ms timer. Active work retains four/eight 4KiB read/write budgets and an owned
single-shot cooldown. Disabled/queued stale activations cannot bypass it.
The 128,000-character renderer backlog pauses reads until consumption; queued
capability replies/paste retain progress and exact partial-write offsets.
Close disables/disconnects watches before handing the descriptor to existing
owned background cleanup. Only reported notifier setup failure uses the old
polling fallback. Thirteen owned socketpair checks pass, including actual fd
number reuse, Unicode boundaries, EOF, start failure and no idle reads/ticks.
Seven existing Linux PTY/runtime checks also pass.

`LAUNCHER/` contains portable Windows/Ubuntu entries and explicit availability
notices for unfinished macOS/Fedora/Mint ports. Windows ARM remains refused.
The Windows forwarding path preserves empty arguments, quotes, trailing slashes
and literal percent names through both WSH layers, including runtime preflight
and repair dispatch. Isolated fixtures never execute setup/repair/installers;
timeout coverage retires owned descendant processes before fixture cleanup.
Release archive validation requires all seven entries and native executable
permissions. See [the current platform matrix](PLATFORM_VERIFICATION.md).

Fresh local continuation checks also pass: responsive **20/20** (135 terminal
layouts, callback depth one, no idle layouts), resource performance **32/32**,
runtime **46/46** plus editor recovery, launcher **17 passed/7 Windows skips**,
and release source/archive policy **7/7**. The native Windows resize fixtures
now dispatch the real event loop because `update_idletasks` alone does not
deliver Windows size events; pure debounce checks remain independent.

## AI edit highlight correction

The reported blue removal was the existing replacement display: shared AFTER
rows were classified as modified and painted blue, without a separate visible
removal marker. Replacement AFTER rows now use the addition's green, with a
separate red boundary/gutter marker for the original text. Pure deletion anchors
stay red without tinting the surviving source line. A deleted file's retained
original view is red. All three themes refresh actual moved ranges and ruler
colors while preserving models, dirty text, undo and review state.

Original-line metadata is added to replacement hunks without changing counts,
change types, revision checks, Accept/Reject or history authority. Red markers
offer a read-only original-text hover: at most 20 lines / 4 KiB UTF-8 per preview,
32 previews and a 256 Ki-code-unit original prefix. Failed prefix lookup marks
the scan exhausted so many later hunks cannot rescan the same long tail.
Untrusted indented Markdown code, with HTML disabled, keeps source markup and
backticks literal. No view zones, file writes or recovery-journal access are
added. The existing bounded fallback remains a conservative changed-middle
comparison; its replacement display follows the same red/green policy.
AI highlights are static; continuous glow animations have been removed.

Expanded CI at `c8a1377`
([39648393272](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39648393272))
passes all four native Ubuntu 24/26 AMD64/ARM64 browser jobs and both macOS 15
offline component jobs, including five viewport scales, Arduino colors and AI
diff rendering. Windows ARM records WebEngine absence without running renderer
checks. Both Windows AMD64 jobs expose a WSH forwarding problem; their initial
failure stays recorded separately from the following correction. Git initially
staged new executable launcher files as 0644 because `core.filemode=false`;
`c8a1377` explicitly records 0755 and adds a Git-mode regression. A real committed
`git archive` export passes all seven entry/content/mode checks locally.

The Windows correction separates the initial WSH parser from internal forwarding.
The portable entry preserves values it has actually received through a child-only
PROCESS payload. Startup validates at most 1,024 arguments / 32,767 UTF-16 units,
then clears all reserved fields before Bootstrap. A marked malformed/oversized
payload refuses before setup; direct unmarked invocation retains ordinary WSH
arguments. Only final Python launch uses CRT quoting. Native fixtures separately
characterize the invoking WSH parser, seed arbitrary already-received quote,
newline and emoji values through production forwarding, and check actual copied
entries with no arguments and valid moved project paths. They assert exact values
and no payload variables reaching Python; no quote cases are silently dropped.

At `2b79f98`, the Windows-only run
[39650299818](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39650299818)
passes the complete native browser/Tk appearance and performance step on both
Python/Qt ranges, including five viewport scales, Arduino syntax, AI red/green
rendering and C startup. The actual moved-path and no-argument Windows entries
also pass. Two remaining failures are confined to new fixtures: a case-insensitive
VBScript variable/class name collision in seeded arguments, and an assumed
fitting combo label that overflows native Windows fonts. `3331153` corrects the
fixture class name and measures native font/edit-field geometry at default and
24-point fonts. Failed launch fixtures now retain at most 4 KiB per output stream
while continuously draining and cleaning up owned processes. Product source is
unchanged from `2b79f98`.

At `3331153`, the focused run
[39652819135](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39652819135)
passes both Windows launcher suites (15 executed / 9 platform skips each) and
all seven release-source checks. Seeded literal quotes, empty arguments,
Unicode, newline values and payload cleanup now pass natively. The run still
fails only the new 24-point font fixture: even “Port” measures 128 pixels in
117 available pixels on the hosted Windows font. The assertion correctly
identifies overflow; a fitting sample must be selected from actual metrics.

The focused rerun selector skips the already-passed native Windows painting step
only when explicitly requested. Default workflow runs retain the complete host
matrix and renderer checks. Launcher/archive and Qt resource checks remain
independent, so a failure cannot suppress the other evidence.

The final focused run
[39653405199](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39653405199)
at `91632a1` passes both Windows Python/Qt ranges: launcher **15 passed / 9
platform skips**, release policy **7/7**, Qt resources/marquees **32/32** each.
The font fixture selects a nonempty fitting label from actual metrics for each
font, then independently asserts long → fitting → long overflow/timer behavior.
It also passes the exact 128-pixel/117-pixel local reproduction. The correction
is fixture-only; no production timer policy or teardown behavior was relaxed.

Product hashes confirm the editor, AI review, terminal, Board/Library and Controls
sources still match the earlier passing Ubuntu/macOS checkpoint. Windows native
painting remains the separately passed `2b79f98` step; it was deliberately skipped
in the final focused run. Product Windows launcher source also remains at
`2b79f98`; subsequent corrections affect fixtures and workflow selection only.
This is combined evidence from explicit checkpoints, not a claim that the last
workflow reran every platform or that unsupported application ports are ready.
