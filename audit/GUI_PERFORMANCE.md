# Board/Library responsiveness and retained sleep state

21 October 2026, Asia/Manila. Scope: the current Python/Tk package browser and
Python/Qt Controls, alongside the unfinished native Rust migration. The user
reports lag in opening, searching, scrolling/resizing and selection, authorizes
backend changes, requests removal of Quit and asks to preserve lists in RAM.

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
