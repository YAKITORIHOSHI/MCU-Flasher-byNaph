# Lite verification — updated 2026-11-14

## November 14 — first-come serial port ownership

A reported takeover was traced through the kernel log: the CH340 board dropped
off USB, the main app released its claim on disconnect, and Lite could then take
the port until a replug. Claims now survive drops on both hosts. Ninety-seven
Lite worker checks include a dropped port staying reserved and its return being
reselected without connecting; main's Ubuntu-ports, runtime, projects (owner
naming, cross-product marking), connection-loss, performance, monitor, upload
and hygiene verifiers pass. The Lite Windows workflow runs the same checks natively.
A real offscreen Qt combo confirms main disables held ports, with keyboard and
wheel skipping them. The Ubuntu All Apps entry for Lite validates with
desktop-file-validate; user-install checks cover setup registration, the
next-launch refresh for earlier installations and portable folders staying out.

## November 14 — multiple windows, modal dialogs and shared ports

Twenty-four Rust checks pass, including modal input blocking (the workspace and a
covered Resources window refuse interaction), Escape, Open sketch fitting its
content (neither 420-point blank space nor collapsed), compact dialog action rows
and independent-window launch arguments. Strict Clippy passes for Ubuntu and the
Windows GNU target. Ninety-five hardware-free worker checks include two real
workers sharing one state directory (the second reuses the running backend
snapshot), the main app's port claims refused in Lite and Lite's visible to the
main app, Connect rechecks, a busy peer settings lock ending a claim within its
budget, one Lite window per sketch, Stop during a package-lock wait and a single
Developer Access portal. History/search (25), preparation (53) and Developer
Access (25) checks and the main port/monitor/upload/project verifiers pass.

Both launchers are rebuilt: Ubuntu x64 with the glibc 2.39 floor through
cargo-zigbuild, Windows x64 as a GNU cross-build with GUI subsystem and icon.
Eleven Glow previews from the rebuilt Ubuntu executable pass on an owned Xvfb.
Native Windows, Windows 11 and physical USB port sharing remain separate gates.
See `evidence/multi-window-2026-11-14.json`.

## November 14 — screen fit, visible portal and cancellation

Local Ubuntu evidence covers the rebuilt Ubuntu x64 release, with the glibc 2.39
floor verified, and a Windows x64 cross-build with GUI subsystem/system DLLs.
Eleven actual Glow/software-OpenGL previews run on an owned Xvfb display: wide
Light/Dark, compact Light/Dark, short diagnostics, compact controls, 360×240
controls/project/board resources, portrait board picker and short Resources.
The guide's preview images come from that rebuilt release executable.

Twenty Rust regressions cover native-scale logical fitting, screen changes,
100–200% simulated widget layouts, persistent Stop/phase transitions, card and
scrollbar clearance, monitor rims, dialog bounds, foreground permission policy,
history and stable streaming. Locked check, strict Clippy and formatting pass.
Hardware-free Python checks cover worker Stop authority, real silent/dripping
child cancellation/deadlines, exited-parent descendant cleanup, unrelated process
preservation, publication races, isolated histories and the real portal helper.
Main portal checks exercise actual workspace input during create/edit/settings
forms and notices, maximized reuse, and owner closure during queued work.

Shared main runtime, controls, owner-ticket service, installation, source inventory,
Bootstrap promotion/Ubuntu window, hygiene, taskbar and shutdown checks pass in
owned fixtures. Chromium runtime/shutdown need unsandboxed local IPC; the same
sandbox refusal was reproduced against the unchanged baseline before a passing
isolated rerun. No device, real account, vault or package download was exercised.

The expanded Lite workflow runs both supplied release executables' actual native
Rust renderer on Windows and Ubuntu, alongside the logical widget matrix, Python worker/preparation/history
checks and real Qt helper. Native Rust host DPI, forced native Qt scale and
simulated egui scale evidence are distinct. Updated native Windows CI evidence
will be recorded after the repository push; Windows 11, Wayland/mixed-monitor
interaction and physical USB remain separate gates. See
`evidence/screen-and-access-2026-11-14.json` for local receipts and artifact hashes.


## Windows interaction repair

Thirteen Rust checks cover actual logo pointer events (including five releases in
one frame and a discarded layout pass), the main-style board picker at five
fractional scales, readiness and stable streaming monitors. All 98 existing
Python checks pass. Twenty Developer Access checks now run the actual helper
entry point in a separate process with fixture accounts and blocked vault/network:
ready acknowledgement, literal activation, one window, responsive authentication
and clean close. This exposed and fixed a buffered-stdin shutdown crash.

Both supplied launchers are rebuilt. Strict Clippy, formatting and main runtime
checks pass. Owned Xvfb captures cover the revised picker and existing themes.
The Lite Windows workflow runs Rust natively and the real Qt helper at five scales
on Python 3.11/3.14. Both jobs passed on Windows Server 2025:
13 Rust checks, strict Clippy, 47 worker checks, 25 history/readiness checks and
23 preparation checks per runtime (three symlink cases require privileges the
runner lacks). All ten real portal launches passed startup, activation, auth
layout and clean close; each reported itself active, including at 200%.
See [the native run](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/43453179689)
and evidence/windows-repair-native-2026-11-13.json. Windows 11, the native Rust
renderer/driver interaction and physical USB remain separate manual gates. See evidence/windows-repair-2026-11-13.json.

## Consistent colors

- One theme drives all fields, combos and menus. A Rust check opens a real
  dropdown in both themes and asserts the shared surface/outline, hover without
  box changes, and at least 4.5:1 contrast for text on every surface and button tone.
- 9 Rust checks, strict Clippy, formatting and 111 Python checks (48 base,
  25 preparation, 25 history/search, 13 Developer Access) pass. 11 previews were
  rendered on an owned Xvfb display.
- Both launchers are rebuilt (Ubuntu with the glibc 2.39 floor). Native Windows
  and physical USB qualification remain pending.

See `evidence/colorway-2026-11-13.json`.

## Active labels, colored controls, Developer Access and Bootstrap inclusion

- Mode labels/SVGs now describe the active theme; the tooltip names the switch.
  Connection has mint/teal backing and yellow/coral/blue/green inputs. An actual
  egui dropdown test covers inherited popup material/text in both themes.
- Nine Rust checks cover the existing readiness/monitor behavior, real popup
  styling and exactly five click releases followed by an 800 ms pause. Four,
  six, slow and already-consumed gestures do not open Developer Access.
- 98 existing Python checks still pass. The additional Developer Access verifier
  constructs the real shared Qt portal with fixture accounts and blocked vault/
  network calls, checks its authentication gate, masked password, Lite-only local
  state and vault namespace, and verifies one-child activation and launch errors.
- The main GUI/build backend remains unchanged. At the user's explicit request,
  shared Bootstrap/release inventory code now includes Lite and future public
  files using tracked checkout paths or a generated release manifest. Legacy ZIP
  fallback includes Lite while rejecting its runtime/build scratch. Installer,
  release-source and main runtime checks use disposable fixtures only.
- Both launchers are rebuilt. Native Windows and physical USB qualification
  remain pending; the existing release is updated in place as requested.

See `evidence/access-2026-11-13.json` for the exact checks, hashes and captures.

## Compact layout, themes, source-folder links and logging

The current launchers include a compact header and action row, separate bordered
Status/USB Port containers, expanded monitor height and Connection-owned serial
send controls. Promotional banners/footer are removed. Light is the launch
default; the SVG mode button changes the complete palette without reloading fonts.
Source filename links launch the original folder's native file manager on a
background thread. Preview never launches a file manager.

- **98 hardware-free Python checks pass:** 48 base integration checks plus the
  existing 25 preparation and 25 history/search/readiness checks. New coverage
  verifies padding preservation when removing decorative marks and delivery of
  original and legacy operation-owned progress identities to Rust.
- **Seven Rust regressions pass:** existing readiness/query/540-frame layout
  coverage, progress-row replacement with preserved diagnostics, theme switching,
  actual monospaced table-column alignment, contrast of every log tone in both
  palettes, literal directory arguments and missing-folder reporting. The monitor
  must use its available height, with no reserved empty footer below it.
- Both launchers rebuild with locked dependencies; strict Clippy, formatting,
  Python syntax and shell syntax pass. Ubuntu x64 retains the glibc 2.39 floor;
  Windows x64 is a cross-build with its dedicated icon and GUI subsystem.
- Actual software-OpenGL captures on an owned Xvfb display cover Light/Dark,
  compact Light/Dark, Serial Light/Dark, source filename links, recent sketches,
  recent boards and Resources in both palettes. Examples are synthetic; no
  Python worker, settings, device or native file manager is accessed by preview.

See `evidence/interface-2026-11-13.json` and its referenced logs. Original
application files remain unchanged. The earlier real Uno/Nano build and reuse
qualification remains the compiler checkpoint; this change does not alter build
or upload decisions. Native Windows/file-manager interaction, Wayland and physical
USB/serial qualification remain pending.

## History, search and monitor follow-up

This checkpoint updates only `LITE-MODE`. Both supplied native executables are
rebuilt for the GUI changes; earlier checkpoints below retain their own hashes.

- **95 hardware-free Python checks pass:** 45 existing integration checks,
  25 preparation regressions and 25 new history/search/readiness checks. The new
  coverage includes 10-sketch/5-board limits, recent-first deduplication, original
  source paths, fresh-process persistence, private settings locks, missing-folder
  pruning, exact/multiple-field/typo search, revision invalidation, library ranking
  and rejection of missing boards/ports before package preparation.
- **Three Rust regressions pass:** action readiness, stale-query rejection and
  actual egui monitor layout over 540 streaming frames. Two monitor widths
  (350/778 logical pixels, covering compact and wide control rows) and
  three display scales cover long lines, partial chunks, tabs and Unicode.
  Viewports and row heights remain fixed; text stays at the left edge and output
  does not change the horizontal scroll position. No native window or Python
  worker is started by these checks. Locked strict Clippy and formatting pass.
- **Real compiler qualification:** Uno builds succeed initially, unchanged and
  after a source edit. Only the sketch object recompiles after that edit. A Nano
  build uses a distinct workspace and preserves the Uno firmware bytes. Initial
  preparation/build took 4.76 seconds; unchanged/edit and Nano actions took
  approximately 1.38–1.44 seconds on this host with compatible main packages.
- **Nine actual backend reuse decisions pass** with only the final device
  boundary mocked. Returning to either board reuses its certified output. Source
  changes reject reuse for both boards; corrupt Uno firmware rejects Uno reuse
  while Nano remains reusable. Restoring certified bytes permits reuse again,
  and Clean forces compilation on the next Upload. Lite enables automatic reuse
  on every Upload even when the main action previously cleared its flag.
- **Six actual GUI captures pass** on an owned Xvfb/software-OpenGL display:
  normal, compact, Serial, recent sketches, recent boards and compact Resources.
  Preview paths are explicitly synthetic. The normal capture updates
  `assets/preview.png`; captures are in `temp/lite-followup-*.png`.
- **Rebuilt launchers:** Ubuntu x64 retains the glibc 2.39 floor. Windows x64
  cross-compiles with its dedicated icon resource and Windows GUI subsystem;
  imported DLLs are Windows system libraries. Native Windows execution and all
  physical USB/serial qualification remain pending.

See `evidence/workbench-2026-11-13.json` and its referenced verification logs and
compiler record. No physical port was opened or firmware uploaded. Original
application files, sketches and protected caches/journals were not modified.

## Automatic preparation follow-up

The original 43-check checkpoint below remains the initial implementation
record. The missing optional AVR uploader reported by the user is now covered
by automatic preparation before both Compile and Upload. This follow-up changes
only Lite's Python tools, checks and documentation; the native GUI executables
and all original application source remain unchanged.

- **45 base integration checks and 25 preparation regressions pass.** The latter
  use owned fake packages, prevent all downloads and mock device operations.
  Coverage includes optional uploader dependencies, main-store byte preservation,
  independent copies, host/version rejection, no legacy metadata writes,
  interrupted-copy exclusion, download-only-missing behavior, cancellation,
  failed preparation and exactly-once Upload handoff.
- A clean private Uno fixture reused five compatible packages from the main
  app: atmelavr 5.3.0, SCons 4.41101.0, AVR GCC 1.70300.191015, Arduino AVR 5.4.0,
  and avrdude 1.60300.200527. No package download was needed. The actual main
  store was guarded against Python mutations during the copy.
- Real compilation and an unchanged repeat succeed. A source edit retains
  framework objects and rebuilds only the sketch object. The captured first run
  including reuse took about 4.7 seconds; unchanged/edit actions including the
  automatic dependency check took about 1.4 seconds on this host.
- The real offline SCons uploader recipe resolves avrdude with its matching
  configuration after preparation. Serial enumeration is mocked empty and
  `Serial.open` is refused in that child. The final Upload callback is mocked
  and verified to run once, so this does not flash a connected board.
- Locked Rust check/Clippy still pass. No Rust or GUI behavior changed, so the
  existing executable hashes and rendering evidence stand. Native Windows and
  physical USB qualification remain pending.

See `evidence/automatic-preparation-2026-11-13.json`,
`evidence/preparation-verification.txt` and the uploader recipe/qualification
logs recorded alongside it. The recipe record omits the inherited environment.
The fake-copy fixture can emit a harmless `xdg-user-dir` diagnostic because its
private XDG path contains spaces; the checks complete without device or network
access. This is separate from native Windows evidence.

All implementation writes are confined to `LITE-MODE`. This work did not edit
the main application's files. Concurrent unrelated changes in the parent
checkout were left untouched. No physical port was opened, firmware uploaded,
reset waveform run, original sketch altered or original cache/journal cleaned.

## Completed locally

- **43 hardware-free Python integration checks:** root source capture and
  deletion, source preservation, exact board catalog search, empty initial
  selection, delegation of all three actions, reset-free baud change, serial
  send/connection dispatch, operation/catalog guards, literal command allowlist,
  bounded protocol, Unicode preservation, real Clean on disposable owned state,
  AI-history preservation, private settings, Python child startup, original-file
  write refusal, real handshake, exact command acknowledgements, malformed JSON
  and graceful shutdown. OS fixtures admit explicit Ubuntu 24.04 and reject
  Fedora/ID_LIKE, malformed releases, macOS and Windows ARM64.
- **Rust:** locked native check, strict Clippy across all local targets, format
  check and release builds pass. Windows x64 GNU cross-compilation passes with
  the dedicated multi-size icon resource and Windows GUI subsystem. It imports
  Windows system DLLs, without an additional MinGW runtime DLL requirement.
- **Rendered GUI:** actual eframe/Glow rendering through an owned Xvfb display
  and software OpenGL, without starting Python or touching settings/hardware.
  Captured normal 1140×800, compact 700×560, Serial and Resources views, including
  the Resources window on the compact viewport. Resources
  stays in the primary row, and compact monitor status/controls have separate
  rows. Captures are retained in `temp/lite-*.png`; `assets/preview.png` is the
  normal preview. The preview labels itself and supplies no fake device state.
- **Real tool preparation and compilation:** a private `atmelavr:uno / arduino`
  fixture downloaded its toolchain, produced `firmware.hex`, and passed the
  backend's output certificate. Subsequent unchanged builds compiled no
  objects. A source edit retained framework objects, either rebuilding the
  sketch object or reusing that exact object's byte-verified shared cache.
  Warm builds took approximately 0.85–0.90 seconds on this host. Port enumeration
  was mocked empty throughout. First-run compiler time is not represented by
  those warm timings.
- **Real Library Resources:** searched the registry for the exact
  `bblanchon/ArduinoJson` package, installed version `7.4.3` in the qualification
  fixture and confirmed `ArduinoJson.h` is present. This did not populate the
  normal user's Lite package store or the main app's library store.
- **Launchers:** native Ubuntu x64 ELF and Windows x64 PE executables are supplied
  at this folder's root. The executable shell launcher passed all five preview
  runs on the private display. The Ubuntu executable is linked with a glibc 2.39 floor,
  rather than the development host's 2.43; its required symbols were inspected.
  The shell launcher passes `bash -n`. Native startup from a file manager has not
  been independently exercised.

The first real compile exposed an asynchronous catalog refresh changing target
metadata before certification. Its refusal was retained as a development result;
Lite now holds operation readiness until catalog refresh settles. Repeated final
builds and the catalog-ownership regression pass. Main backend source is unchanged.

The record in `evidence/2026-11-13.json` includes artifact hashes, source hashes,
the copied backend's source manifest, compiler receipts and library evidence.
Large downloaded build toolchains and generated Cargo output are development
scratch, not part of the application source or launch bundle.

## Still unqualified

- Native Windows 10/11 startup, input, dialogs, scaling, driver behavior and USB.
- Native Ubuntu 24.04 and Ubuntu ARM64 execution, despite the explicit host policy
  and the x64 executable's compatible glibc floor.
- Wayland interaction and mixed-monitor/DPI behavior.
- Physical upload/restart, serial traffic and driver transients on either OS.
- All-board toolchain coverage. The real compiler fixtures cover AVR Uno and
  Nano; the broader catalog supplies identities, not per-board qualification.

These are platform/hardware evidence limits, not claims that the Windows
cross-build or a virtual display constitutes native desktop or USB testing.
