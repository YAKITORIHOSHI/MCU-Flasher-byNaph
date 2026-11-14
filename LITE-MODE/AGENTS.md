# Lite development

All implementation and documentation changes for Lite belong in this directory.
Keep the main build backend, runtime, user sketches and protected caches unchanged.
Explicitly requested shared Developer Access/Bootstrap GUI fixes belong in their
main application sources. The user authorized shared Bootstrap/release ownership integration in
application_sources.py, user_install.py and build_release.py plus their guidance.
Public files must enter future installs automatically through Git/release manifests. This is a separate Rust application, not the desktop migration.

The GUI uses egui/eframe, paper/charcoal palettes, heavy ink outlines, hard offset
shadows and original SVG assets. Never add decorative emoji or icon fonts.
Only Ubuntu 24.04+ and Windows 10/11 x64 are admitted. Keep native host claims
separate from cross-compilation and physical USB verification.

Python is a headless tool worker over bounded JSON lines on stdio. Never expose
arbitrary module names, methods, commands or shell strings over that protocol.
Reuse the main backend from a private source snapshot. Changes to the original
backend should be adopted on the next worker start; do not fork its build logic.
Private settings, source snapshots, build state and packages live in Lite's own
per-user state directory. Clean must operate only there. Original source folders
are read-only; resnapshot their root sources before Compile and Upload.

Compile/Upload automatically prepare the exact selected board's build and upload
dependencies, including optional uploaders. Capture the main native tool store
before redirecting Lite's environment. Reuse only host/version-compatible packages
through verified independent copies; never claim legacy main packages by writing
metadata, link writable tools to the main store, or download into it. Keep partial
reuse stages outside package-manager search directories. Stop/failure during
preparation must prevent the pending action; successful handoff occurs once.

Reuse the main history helpers against Lite's settings and private settings lock:
10 original sketch paths and 5 exact board names, bumped first without duplicates.
Never save staged source paths as recent projects. Keep board search indexed by
catalog revision, bound results, rank exact matches first and label typo matches.

Compile needs a sketch and valid board/framework. Upload also needs a selected
connected port. Disable actions in Rust and recheck in Python before preparation
and handoff. Enable the main backend's certified Skip Compile on every Upload,
without a checkbox; never bypass its source/dependency/output validation or merge
the exact-board/framework workspaces. Keep monitor rows and viewports fixed,
scrollbars reserved and widths stable while streaming. Preserve user scrollback.

Several Lite windows may share one state directory. Each worker holds
workers/<pid>.lock for life and keeps scratch under sessions/<pid>; refresh the
backend snapshot under engine.lock only when no other worker is alive (the
manifest is written last), serialize package changes with packages.lock and keep
one Developer Access portal (developer-access.lock). Claim sketches through the
backend's instance registry (one Lite window per sketch). Serial port claims also
read the main app's live instances and main reads Lite's: each product writes only
its own registry, and claims only try the other's lock so neither deadlocks.
The first selector keeps a port: a USB drop keeps the claim and Lite reselects
its returning port without connecting; only another port, No port or closing
releases it. Both hosts use the same claim code.
Choosing another sketch while one is open launches an independent window with
`--project`. Project, board and Resources dialogs are modal: the top open dialog
is egui's modal layer over a dimmed backdrop, the board picker stays above
Resources and Escape closes the top one. Dialog action rows use `action_row()`;
a centered layout given a scrolling window's remaining height would spread its
buttons across it. Open sketch fits its content instead of egui's 420-point
default.

Light is the launch default; the SVG Light/Dark button changes visuals without
reinitializing fonts, logs or selections. Keep Status and USB Port in separate
fixed-height containers.
The mode label/icon describe the active theme; its tooltip names the switch.
One color role each: yellow = primary action or selected tab, coral = Stop,
Disconnect and errors, everything else the shared surface with an ink outline.
Fields, combos and menus use the global theme; never give one control its own
accent fill. Raised buttons come from `button()` and tabs from `tab()`.
Keep Stop present in every state and use worker `stop_state` authority for its
enablement/tooltip. Pack whole button scopes before painting; wrapping a scope
after custom children are drawn leaves controls outside the row. Apply native DPI
once, refit on monitor changes, and keep narrow Monitor/Sketch & Connection views.
Reserve solid scrollbar gutters and card-shadow clearance. Short monitor layouts
and project/board/Resources windows scroll without shrinking saved fonts or logs.
Board rows match the main picker: 48 logical pixels, left-aligned title/subtitle,
full-width hit areas, bounded truncation with tooltips and deduplicated recents.
Click selects a candidate; Select Board, Enter or double-click confirms, arrows
navigate and Escape cancels. Preserve the configured target until confirmation.
Verify compact and wide windows at 100/125/150/175/200 percent.
Five completed logo clicks and an 800 ms pause open the existing main Developer
Access portal in an owned helper. Keep its authentication gate, Lite-only cache
and credential namespace, single-window activation and graceful closing. Count ordered pointer releases
once per frame, including batched events and layout retries. Wait for the helper's
ready acknowledgement before the exact-PID Windows foreground permission handoff.
On Windows explicitly launch the helper with SW_SHOWNOACTIVATE as well as
CREATE_NO_WINDOW, overriding the borrowed backend's SW_HIDE startup hook. Ready
requires actual native window visibility. Dispatch activation only after a
successful foreground grant and preserve maximized state on repeat gestures.
Validate an owned descendant when Windows venv Python redirects;
never grant ASFW_ANY or take focus from another app. Close accepted standalone
portals cleanly; their daemon command reader must not hold a buffered stdin lock. Never
launch the real portal, accounts, vault or network in fixtures; use the real UI
with injected fixture services through tools/verify_developer.py. Its subprocess
fixture exercises the real helper entry point and literal activation/close;
native display is allowed only on isolated Actions runners. Keep the Windows
workflow and evidence distinct from Windows 11 and physical USB qualification.
Library registry searches use only python/registry.py's fixed endpoint through
the cancellable owned tool runner, with a 30-second absolute budget and bounded
metadata. Serialize Stop with result/success publication and compile/upload
handoff. Assign Windows children suspended to the shared owned Job helper;
reserve POSIX leader identity until group retirement. Never use unverified PID
taskkill or retire another process. Check silent/dripping/exited-parent cases.
Avoid promotional banners, footer slogans and unused
monitor reservations. Serial send controls stay in Connection.
Source filename links open the original source directory via the native file
manager off the GUI thread, with errors reported in Lite. Preview must never
launch it. Honor operation-owned progress keys without replacing diagnostics;
removing decorative marks must retain boxed-table padding.

Use `cargo check --locked`, `cargo clippy --locked --all-targets -- -D warnings`
and `cargo test --locked`, plus `python -B tools/verify.py`,
`tools/verify_preparation.py` and `tools/verify_history_search.py` for behavior
changes. The Rust checks render streaming monitors without a native window.
The verifiers use owned
temporary fixtures; never compile, clean, download into, or probe live projects
or attach real serial devices during verification. Save evidence in `evidence/`
and scratch/captures in `temp/`.
`tools/verify_layout.py` renders synthetic previews on its own Xvfb display or
isolated Windows Actions only. Add small/short/portrait captures and distinguish
native renderer evidence at host DPI from the 100–200% simulated widget matrix.
