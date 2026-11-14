# Lite 0.1.0 — updated 2026-11-14

Screen fit, Developer Access and cancellation repair:

- Show the owned Windows portal even with the borrowed backend's subprocess
  hiding hook installed; report readiness only after native window visibility.
- Keep the main application usable while Developer Access and its window-scoped
  forms/notices are open. Reopening preserves maximization and closes safely with
  the owning workspace.
- Keep Stop visible in every state, with backend-authoritative enablement and
  reason-specific tooltips. Protected firmware writes/restarts and cleanup finish normally.
- Fit scaled/portrait/short desktops, pack complete action rows, reserve visible
  scrollbar gutters outside Sketch/Connection cards, and keep monitor rims inside
  the pane. Narrow windows offer Monitor and Sketch & Connection views.
- Bound project, board and Resources windows and make short contents scrollable.
  Preserve log history, selections, fonts and theme state while changing layout.
- Run library searches in an owned helper with a 30-second elapsed budget;
  cancellation cannot publish late results or success. Retire owned Windows Job
  descendants or Ubuntu process groups without affecting other programs.
- Bootstrap switches between Technical details and Activity summary with the
  button naming the view it will open.
- Add actual Rust renderer previews to Windows and Ubuntu CI alongside the
  100–200% simulated widget matrix and real Qt portal checks. Windows 11,
  mixed-monitor interaction and physical USB still need their own evidence.


Windows interaction repair:

- Match the main board picker: 48-pixel left-aligned rows, bounded/truncated
  titles and subtitles, deduplicated recents, candidate selection, Select Board,
  double-click/Enter confirmation, arrow navigation and Escape cancellation.
- Count every completed logo click even when a slow native frame batches several
  releases; prevent duplicate counting during a layout retry.
- Reuse the current main Developer Access portal with a startup acknowledgement,
  an owned Windows foreground permission handoff, and useful startup diagnostics.
- Resolve Windows venv redirectors to the verified GUI child PID for readiness
  and foreground permission; reject unrelated processes and ASFW_ANY.
- Close the standalone helper on an accepted portal close. Read its narrow command
  protocol without a buffered-stdin daemon lock during Python shutdown.
- Windows Server 2025 CI passes for real helper processes and native portal layouts at
  100/125/150/175/200 percent, plus Rust input/layout and worker regressions.
- Include Cargo.lock in source installs and archives for reproducible --locked builds.
- Preserve the shared paper/charcoal/yellow colorway and main authentication rules.

Consistent colors:

- Each color now has one meaning. Yellow marks primary actions and the
  selected tab, coral marks Stop, Disconnect and errors, and everything else uses
  one paper surface with an ink outline. The mint Connection panel and its
  coral, blue and green fields are gone.
- Recent sketches/boards, Copy, Clear, Send and Dismiss now match the other
  buttons, and icons follow the theme's ink in Light and Dark.

Access and color follow-up:

- Theme labels/icons now show the active mode. Light shows Light mode; Dark shows
  Dark mode. Hover text explains the switch to the other theme.
- Colored Connection's panel and board/framework, port, baud and upload-speed
  controls, retaining readable popups and existing readiness rules.
- Five logo clicks followed by an 800 ms pause open the main Developer Access
  portal. Reuse its authentication and ticket behavior with Lite-only local
  state and credential namespace; repeat gestures foreground one owned window.
- Include the supplied native executables in committed source and release ZIPs;
  keep development evidence, private runtimes and build scratch out of bundles.

Layout and logging follow-up:

- Click a sketch source filename to open its original folder in the native file
  manager; launch it off the GUI thread and report launch failures in Lite.
- Removed promotional banners/footer, reduced header and panel spacing, and
  expanded monitor output into the available height. Serial send controls now
  belong to Connection. Status and USB port details each have their own container.
- Added a Light/Dark mode button with dedicated SVG sun/moon assets. Light is
  the launch default; both palettes cover the workspace, monitors and dialogs.
- Honor uploader progress identities and legacy replacement hints, so updates
  refresh their activity row instead of duplicating it. Preserve diagnostic
  history, blank separators and table padding when removing decorative marks.

Workbench follow-up:

- Added persistent recent sketch and board menus, with the main app's 10-folder
  and 5-board limits, duplicate removal and most-recent-first ordering.
- Added indexed board search across names, IDs, vendors, platforms, MCUs and
  frameworks; multiple-word matching, exact-match ranking and typo suggestions.
  Library results prioritize exact names and exclude duplicate packages.
- Stabilized Build/Serial output with fixed virtual rows, reserved scrollbars,
  stable content widths and equal monitor viewport heights.
- Compile requires a sketch and board/framework. Upload also requires a selected
  connected port. Enforce readiness in the GUI and again in the worker.
- Enabled automatic verified Skip Compile on Upload, including after Clean.
  Preserve each sketch's separate board/framework builds; changed source,
  dependencies or damaged/missing firmware force compilation.
- Rebuilt the supplied Ubuntu and Windows executables for these GUI changes.
  Real Uno/Nano builds and nine mocked upload-boundary decisions qualify reuse;
  physical USB and native Windows verification remain pending.

Preparation fix:

- Compile and Upload automatically verify and prepare the selected board's full
  build/upload dependency set before dispatch. No manual preparation is needed.
- Fixed missing optional upload packages, including AVR `tool-avrdude`, by using
  the platform's actual upload-target dependency configuration.
- Reuse compatible main-app packages through verified independent copies;
  download only missing requirements. Keep original package files untouched.
- Stop cancels preparation before any upload starts; failures never replay an
  upload. Interrupted package copies cannot be mistaken for installed tools.
- Removed main-workspace tab suggestions from Lite logs and updated missing-tool
  instructions for automatic preparation. Existing native launchers load the
  updated Python tools after Lite is closed and reopened.

- Added an isolated Rust/egui workbench and headless Python tool worker.
- Reused Compile, Upload, Clean and passive monitor behavior from the existing
  backend through a private source snapshot, settings and tool store.
- Added root sketch snapshots, board/framework search, ports, upload speed,
  separate Build/Serial monitors and serial send controls.
- Added Library search/install and selected-board native tool preparation.
- Added a paper/yellow/coral neobrutalist interface, hard shadows, original SVG
  controls and a dedicated chip/bolt application icon, PNG and Windows ICO.
- Added Ubuntu and Windows executable launchers, optional private Python setup,
  reproducible native builds, integration checks and incremental-build fixtures.
- Held build dispatch through catalog refresh and matched command acknowledgements
  so stale status messages cannot unlock targets or prematurely permit closing.
- Kept all new implementation and documentation in LITE-MODE. No shipping main
  source, sketch, original runtime or protected journal is changed by this work.

Windows native desktop/USB verification and the additional Ubuntu host gates
listed in VERIFICATION.md are pending; cross-compilation is recorded separately.
