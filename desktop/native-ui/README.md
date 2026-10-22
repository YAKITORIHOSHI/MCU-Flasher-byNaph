# Native Rust workspace development preview

This is a native-widget development preview for the requested Rust GUI. It is
separate from the existing Tauri web-shell fixture and the production Python
application. It does not change either entry point.

The [required replacement platform matrix](../../audit/migration/PLATFORM_SUPPORT.md)
now includes Windows 10/11 AMD64/ARM64, Ubuntu 24.04/26.04 LTS AMD64/ARM64,
macOS Intel/Apple Silicon, Fedora x86_64/aarch64 and Linux Mint AMD64.
The current preview has only GTK/Linux and Win32 adapters; macOS exits with
an unsupported-host diagnostic. Windows ARM64, Fedora and Mint have no recorded
native execution here. Existing Windows Server AMD64 / Ubuntu 24.04 hosted
checks and local Ubuntu 26.04.1 AMD64 checks remain their exact evidence.
New host adapters, matching firmware tools, desktop/device qualification and
packaging are required before a support claim or production launcher switch.

Native-UI caption checkpoint `6ef749a` differs from CI-tested `0e0db8a` only by the “Select serial
port” caption (strict MSVC/fmt checks). The preview has real GTK3 and Win32 toolbar/Controls, source-tab
and seven-tool-tab chrome around two retained embedded editor/terminal views.
The shared Rust design module copies the original 39 palette tokens across
Glass Smoked Dark, Glass Frosted Light and Solarized Dark (117 exact color
values), and both adapters reuse the existing Montserrat font. GTK reuses the
original action SVGs; Win32 reproduces the brand mark through native drawing
paths and still uses text action buttons. Win32 action and tool-tab icon parity
remain unfinished. Monaco and
xterm remain the embedded components; application chrome is native. GTK and
the system browser engine remain native dependencies, and the two embedded
components remain JavaScript. Python is used by developer verification only.

On both hosts, `--project FOLDER` explicitly reads that project's root sources
through the shared Rust `workspace_project.rs` / `mcu_host::NativeProjectSession` on a
worker. Exact ordered document acknowledgements bind native source controls;
dirty updates accept only known document IDs and newer revisions. Native source
tabs select retained Monaco models. On GTK, the native Project picker opens
the chosen folder in a new process, preserving the current window's buffers.
The session continuation retains the native ProjectService, workspace ID and
original document snapshots after that worker returns. One pending or active
read belongs to a window; only exact ordered IDs commit it, and another project
must open independently. Failed dispatch, editor refusal or acknowledgement
timeout releases pending read state. An accepted session stays through themes,
resizing and editor detach/reattach. Renderer scripts move out before browser
dispatch so native session borrows cannot cross reentrant IPC.

Save and Save All now use the retained native source owner on a worker. The
native buttons and Ctrl+S / Ctrl+Shift+S capture bounded editor snapshots;
Save All commits changed files individually and stops on the first failure,
retaining acknowledgements for files already saved. It is not one atomic
transaction across the project. Saved acknowledgements update baselines without
replacing models, undo history, selection or newer unsaved edits. Opening alone
does not write project metadata. The first Save creates a native coordination
file, `.mcu-native-save.lock`, in the selected project.

Each save verifies native root/source identities and exact prior bytes, then
atomically replaces the source with recovery retention for observed conflicts.
Read-only files, links, changed source paths/bytes and unavailable atomic
replacement are refused. Linux also refuses ACL/xattr or ownership metadata it
cannot preserve. Recovery failures retain the transaction path for inspection
and block further writes from that session. The native lease coordinates these
save sessions only; legacy Python coexistence and hostile same-user filesystem
races remain unqualified. No compile, device, settings or live app registration
service is enabled by this change. Only the existing internal `mcu-host`
dependency is added; no new external crate or runtime is installed.

`--save-smoke` independently verifies actual native Save/Save All callbacks on
two owned files, exact BOM/Unicode disk bytes, retained models/undo/selection,
a newer edit during Save All, and refusal of an external disk change. The old
`--workspace-smoke` remains read-only. Local Linux passes 32 Save checks, the
19 workspace and 17 detach checks, plus 256 combined library tests after the
worker-loss correction. Hosted Windows and both Ubuntu 24 architectures pass
the original Save slice (32/19/17 checks each) in run `39823016524`; the later
worker-loss correction awaits its exact-source run. See
`audit/migration/native-source-save.json`.

If a worker stops without returning its native owner, buffers remain open and
the window can close, while further saving requires reopening the project.
The message explicitly warns that some files may already have changed. A
lost result never clears dirty state or reconstructs write authority from stale
snapshots.
Windows accepts `--project`, `--theme` and `--workspace-smoke`. Its new native
Project picker cancels without spawning or launches the current Rust executable
with literal native path/current-theme arguments on a worker; the 16-child cap
and independent process preserve existing buffers. Main/detached windows borrow
owned small/large HICONs decoded from the original ICO through a bounded shared
parser. Decoder storage is DWORD-aligned `Vec<u32>` with exact bytes/length;
the reviewed alignment P2 is fixed. Native Windows units now pass. GTK main/detached windows now reuse
the existing `mcu_icon.png` as their native icon. Unavailable backend actions
remain disabled in manual previews, including the old Windows Compile fixture
action and compact-menu backend entries.

The session continuation locally passes 23 native library tests, 19 workspace
checks (including a retained native project session) and the separate 17-check
detach fixture. Strict Linux and Windows-target linting and formatting pass.
These results are separate from the historical checkpoints below; exact sources
and remaining gates are in [session evidence](../../audit/migration/native-project-sessions.json).
Complete [native CI 39539221917](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39539221917)
at `49ab312` passes all five jobs. Both Ubuntu architectures pass 23 native
units, Windows passes 28, and all three pass 19 workspace/17 detach checks.
Both Windows Python editor jobs pass. Runtime source equals `eb9d13c`; the
follow-up changes only the workflow's required workspace contract.

The earlier GTK workspace checkpoint passed 18 checks for retained models,
dirty content/selection, all three themes, undo/redo, native source controls and
unchanged owned source bytes. Eighteen local native-unit checks pass on Linux,
alongside the separate 17-check detach smoke,
five controller lifecycle checks and three path checks. This is 35 local GTK GUI
checks. Original-size captures under ignored
`audit/migration/captures/native-workspace/` cover dark, light and Solarized at
1100×740, compact 960×540 and `GDK_SCALE=2` measured at 1792×1062 on a 1920×1080
display. Visual review of all five verifies visible native reading spans and
active controls; normal close and unchanged explicit-source bytes also pass.
GTK tool-tab contrast was corrected during that review. Hosted execution of the
styled adapters now passes at `e334d7b`: Windows Server executes 17 native checks
and Ubuntu AMD64/ARM64 each execute 35. These results include the Win32
short-height/DPI corrections, but do not qualify Windows 10/11 desktop input or
mixed monitors. Subsequent logo-ink (`#56cfbf`) and final Win32 glass
`TEXT_BRIGHT` action-ink corrections pass local strict Linux/MSVC-target Clippy;
they are included in `297cae7`, whose CI `39421097096` finishes 4/5. Both Ubuntu
jobs pass; Windows passes three new source-state units, old 17 smoke and recipe
checks, then fails new workspace before assertions at trusted-root canonical/
plain path spelling. The shared fix accepts both trusted app-root spellings
while preserving the literal selected project path; three local reader checks
and GTK 35 pass. The combined prefix/picker/icon checkpoint is now `0e0db8a`;
one-job Windows CI `39426407678` passes 16 library/seven Windows binary units,
17 detach + 18 workspace GUI and recipe 951 + 11 protocol checks. Cancel/Unicode
UTF-16 args, real owned small/large HICON and deliberately unaligned-buffer units
pass; both source files use the Unicode/space fixture. An additional
real GTK 40-tab regression failed with a 6412-pixel top-level window before
source-row scrolling, then passed at `window=960 row=6412 retained_tabs=40`.
The separate 42-source 960×540 capture is retained under ignored
`audit/migration/captures/native-workspace-many-sources/`. Scoped follow-up
review found no new actionable issue after the overflow/manual-action fixes;
the report is `scratch/native-ui/windows-reader-review.md` under the migration
audit. The new picker/icon scoped re-review is clean in
`scratch/native-ui/windows-picker-icon-review.md` after the alignment correction.
Those picker/icon changes added no Cargo dependencies or lockfile entries, only minimal existing
Windows COM/Shell features. Settings, menus, auxiliary screens, tool-tab
icons and live toolbar/service behavior still need exact design parity.

The separate original detach fixture remains available with `--smoke`. It uses
explicitly fictional board names and a **Compile fixture** callback that does
not compile firmware. Its 17 checks passed on Windows Server and Ubuntu
AMD64/ARM64 at `77e5ccb` and subsequent recorded checkpoints. Current Python
application board compilation qualification is a separate campaign. The full
application backend still requires migration and parity qualification: the
67-capability ledger remains 14 partial fixture subsets and 53 pending, with
zero Python application services retired.

## Isolation and resource limits

- All web resources are fixed, compile-time copies of existing repository
  assets. The original Monaco/xterm files are unchanged. A finite custom-origin
  asset allowlist exposes no filesystem path or network proxy.
- CSP blocks remote content, frames, forms and object embedding. Renderer
  navigation, new windows and downloads are refused. Native Project and Detach
  Editor actions explicitly open independent windows.
- Typed IPC accepts reports from the native-bound editor or terminal island.
  It refuses unknown fields, renderer-supplied ownership, the wrong island and
  messages over 16 KiB. Save payloads carry only known document IDs and bounded
  UTF-8 bytes after a native request; paths, ownership, process, serial and
  settings authority remain native. A transfer is limited to 8 MiB per source,
  32 MiB overall and ten seconds, with one active save per window. The shared root-source reader starts only from native
  `--project` selection, or the GTK native Project picker.
- The original native main window remains the fixture owner while only the
  editor view moves. No renderer-supplied owner identifier is accepted. This is
  a UI fixture, not qualification of legacy app coexistence or device services.
- WebKit profiles and process-local XDG data/config/cache folders are created
  atomically under `audit/migration/scratch/` and only that owned profile is
  eligible for exit cleanup. WebKit helpers can recreate empty owned profile
  directories after the Rust process exits; normal manual exit is not a claim
  of zero residual directories. The smoke controller stops its captured
  descendants. No user HOME, shell profile, application settings,
  sketch or protected build cache is changed by verification. Explicit project
  reading does not write sources; manual Save writes only the selected native
  documents and its native lease/recovery entries.
- Selected scratch ancestors must be real directories inside the checkout.
  The developer controller holds no-follow directory handles, refuses existing
  output symlinks, and publishes logs/receipts by atomic leaf replacement. An
  existing hardlink is replaced rather than written through to its other name.
- The smoke has a 45-second native deadline and a 70-second controller deadline;
  controller cleanup targets only captured process groups. It uses non-reaping
  `waitid(WNOWAIT)` observations and retains its direct session-leader child
  until live group members are gone, so the group number cannot be recycled
  during signaling. Exited leaders do not skip surviving children; cleanup
  escalates from SIGTERM to SIGKILL and refuses group signaling if another
  caller has reaped the retained child. This private controller is single
  threaded and does not claim containment of descendants that deliberately
  leave the captured session/group. Terminal history is
  limited to 100 lines and editor workers to four. The probe reuses the existing
  Cargo target directory and locked GTK 0.18.2 / Wry 0.57.0 dependencies.

## Build and verification

From the checkout root, using the existing private development tools and native
GTK3/WebKitGTK 4.1 prerequisites:

```bash
source desktop/dev-env.sh
CARGO_BUILD_JOBS=2 cargo build --offline --manifest-path desktop/Cargo.toml -p mcu-native-ui-probe
.venv-linux/bin/python3 desktop/native-ui/verify-smoke.py
.venv-linux/bin/python3 desktop/native-ui/verify-smoke.py --workspace
.venv-linux/bin/python3 desktop/native-ui/verify-smoke.py --save
```

The controller creates its own private Xvfb display and D-Bus session. It writes
`audit/migration/scratch/native-ui/latest-smoke.json` and `latest-output.log` for
the 17-check detach fixture. `--workspace` writes the same leaf names under
`audit/migration/scratch/native-workspace/` and invokes `--workspace-smoke` with
its own two-file source fixture; it never selects a user project.
This is native X11 functional execution using software rendering; it does not
qualify GPU performance or a GNOME desktop session. Use the existing display
only when explicitly verifying that display:

```bash
GDK_BACKEND=wayland .venv-linux/bin/python3 desktop/native-ui/verify-smoke.py --current-display
GDK_BACKEND=wayland .venv-linux/bin/python3 desktop/native-ui/verify-smoke.py --current-display --workspace
```

That command requires a real accessible Wayland session. No Wayland result has
been claimed from an Xvfb run. On a native Linux desktop, preview the workspace
or explicitly read a project with:

```bash
desktop/target/debug/mcu-native-ui-probe --theme default
desktop/target/debug/mcu-native-ui-probe --theme light
desktop/target/debug/mcu-native-ui-probe --theme solarized_dark
desktop/target/debug/mcu-native-ui-probe --project "/path/to/sketch"
```

The project path is explicit input, not inferred hardware state. Memory edits
are not saved. On Windows:

```powershell
cargo build --locked --manifest-path desktop/Cargo.toml -p mcu-native-ui-probe
& desktop/target/debug/mcu-native-ui-probe.exe
& desktop/target/debug/mcu-native-ui-probe.exe --theme light --project "C:\path\to\sketch"
& desktop/target/debug/mcu-native-ui-probe.exe --smoke
& desktop/target/debug/mcu-native-ui-probe.exe --workspace-smoke
```

Use the pinned development toolchain and installed WebView2 prerequisites;
this command does not install them. The workspace smoke owns its project fixture
and runs separately from the original detach smoke. Native Windows Project
picker/HICON source is included in `0e0db8a`, with actual chooser interaction,
child survival, Windows 10/11 input, high DPI icons and PE Explorer packaging
still separate qualification gates. No installer/runtime entry changed.

Initial verification on 20 October 2026 at `4b42146` passed six
protocol/origin/scratch-root tests and 17 native smoke checks on Linux AMD64,
GdkX11Display and WebKitGTK 2.52.6. The `7da4be0` Windows adapter and fixed-origin
guard bring the protocol checks to seven on Linux and six on Windows; the
scratch-root canary is Unix-only. SDK correction `77e5ccb` also passes all five
migration jobs, including 17 native smoke checks on all three native hosts. The
smoke inspects actual GTK runtime types and callbacks; loads the original C++
grammar; executes a real Monaco diff worker; checks xterm-rendered Unicode;
then moves only the editor through three detach/reattach cycles. It retains
the original native editor widget, main-window owner, live Monaco model and
dirty Unicode content, selection, and working public undo/redo history. The
terminal stays in its original native parent throughout. The no-Python check
inspects this native application's mappings for `libpython`; it does not
qualify the rest of the application's future backend.

The first protocol tests failed at unimplemented validation, then passed. The
initial native smoke failed at the diff-worker readiness boundary: the retained
webpack bundle replaces `MonacoEnvironment`. Installing the bounded local
worker adapter after that initialization fixed the failure without editing the
original bundle. This reuses the same worker-host ordering as the existing
Tauri fixture.

Review regressions also failed before their corrections: an exited session
leader left both ordinary and SIGTERM-ignoring children alive, and selected
output/root aliases redirected writes. Five controller lifecycle checks now
cover survivor cleanup, escalation, retained child identity, an unrelated
session remaining alive and refusal after external reaping. Three path checks
cover root escape, both selected output symlinks and hardlink-safe publication;
the Rust scratch-root canary is part of the Linux protocol checks. All canary files and
directories are created inside owned audit fixtures.

Additional checks:

```bash
source desktop/dev-env.sh
CARGO_BUILD_JOBS=2 cargo test --offline --manifest-path desktop/Cargo.toml
CARGO_BUILD_JOBS=2 cargo test --offline --manifest-path desktop/Cargo.toml -p mcu-native-ui-probe
.venv-linux/bin/python3 -W error desktop/native-ui/verify-cleanup.py
.venv-linux/bin/python3 -W error desktop/native-ui/verify-paths.py
CARGO_BUILD_JOBS=2 cargo clippy --offline --manifest-path desktop/Cargo.toml -p mcu-native-ui-probe --all-targets -- -D warnings
cargo fmt --manifest-path desktop/Cargo.toml -p mcu-native-ui-probe -- --check
```

The previously completed migration-prototype CI matrix runs seven Rust checks, five controller
lifecycle checks, three path checks and 17 native widget smoke checks on
Ubuntu 24.04 AMD64 and ARM64. It reuses the job's GTK/WebKit dependencies,
pinned Rust toolchain and Python runtime. CI supplies its display with
`xvfb-run` and the controller's `--current-display` option, so the checkout-local
Xvfb binary is unnecessary there. The existing per-runner artifact contains
the protocol/controller logs and `latest-smoke.json` / `latest-output.log`.
Windows runs six Rust protocol checks and the actual Win32/WebView2 adapter's
17 native smoke checks. Existing Tauri and editor-review jobs are retained.
All five migration jobs pass at `77e5ccb` in
[run 39357769519](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39357769519).
The firmware-tool hook checkpoint `f249c21` also passes all five focused
migration jobs in
[run 39359319887](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39359319887),
with native Rust UI source unchanged. The subsequent reader/release-selection
checkpoint `8dbdb9d` passes all five focused jobs in
[run 39360040590](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39360040590).
Those results predate the current workspace styling/reader changes. Checkpoint
`e334d7b` adds the Linux `--workspace` CI step;
all five jobs pass at `e334d7b` in the focused
[run 39392866712](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39392866712)
with 17 native Windows Server checks and 35 GTK checks on each Ubuntu
AMD64/ARM64 host. The later `297cae7` checkpoint adds the shared Windows reader,
ordered acknowledgements, native-source overflow fixes and new Windows workspace
checks. Its five-job [run 39421097096](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39421097096)
finishes 4/5: both Ubuntu and both Windows editor jobs pass; Windows native
fails the new workspace at trusted-root path spelling before assertions after
passing old 17/source-state/recipe checks. The prefix fix subsequently passes
the focused 35-check Windows GUI run at `0e0db8a`; historical failure is retained.
Each result retains its actual tested source;
hosted execution does not qualify actual Windows 10/11 desktops or physical USB.

The combined prefix/picker/original-ICO checkpoint
`0e0db8ac2505db273c4750b2b44fca0502d31ed8` runs the focused one-job
[Windows CI 39426407678](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39426407678),
passes its native job: 16 library/seven Windows binary units, 17/17 detach,
18/18 workspace and 951/951 recipe plus 11 protocol checks. The four unchanged successful jobs retain their prior
`297cae7` results; they are not a new full-matrix execution at this checkpoint.
Receipts are under `audit/migration/scratch/native-project-ci-0e0db8a/migration-prototype-windows-2022/audit/migration/logs/`.
Actual chooser/cancel/child survival, Windows 10/11 input/IME and Explorer icon/
packaging remain gates. No popup Escape behavior changed after CI.

## Next gates

Keep `mcu-contracts`, `mcu-core` and `mcu-host` as reusable Rust service layers;
the Tauri adapter can remain a differential fixture. Both native adapters reuse the
core's source reader but do not activate live-write capabilities.

1. Qualify the Linux adapter on actual Ubuntu 24.04+ AMD64 and ARM64 desktops,
   under both Wayland and X11. Hosted native X11 execution already passes on
   both architectures. Wry's supported Wayland route uses GTK containers;
   a raw winit handle with `build_as_child` supports Linux X11 only.
2. Qualify actual Windows 10/11 desktop workflows after the passing Windows
   Server native-widget/WebView2 fixture. Unsupported
   hosts exit with code 78; no adapter falls back to the Tauri application shell.
3. Prove actual keyboard focus, IME, clipboard, accessibility, editor tab order,
   resizing, 100–200% and fractional DPI, maximize/restore, mixed monitors and
   native editor-only detach. Current smoke proves model history through Monaco
   APIs; the workspace smoke adds retained source models and theme changes.
   Complete keyboard routing, recovery, saved fonts and lifecycle behavior remain
   separate gates.
4. Extend the local compact / `GDK_SCALE=2` evidence to supported native desktops,
   fractional DPI and mixed monitors, then complete Settings, menus, auxiliary
   screens, tool-tab icons and saved preferences with
   full responsive and resource budgets.
5. Port the remaining compile/upload, package/index management, serial, PTY,
   settings, discovery, persistence, cloud and lifecycle application services
   to Rust. Python may remain an explicitly invoked firmware-tool dependency.
6. Finish asset notices, packaging, launchers, upgrades/rollback and release
   qualification before replacing the current Python application.

Primary integration references:
[Wry child/GTK embedding](https://docs.rs/wry/0.57.0/wry/),
[GTK reparent API](https://docs.rs/wry/0.57.0/wry/trait.WebViewExtUnix.html),
[WebView2 parent/focus/bounds](https://learn.microsoft.com/en-us/microsoft-edge/webview2/reference/win32/icorewebview2controller),
[Rust GTK3 bindings](https://docs.rs/gtk/0.18.2/gtk/).

The separate Rust compatibility backend milestone is recorded in
[compatibility evidence](../../audit/migration/rust-board-compatibility.json).
It adds bounded owned-fixture metadata reads and Linux Arduino Q policy, with
no native GUI/build dispatch or production package authority. Use the existing
migration workflow’s `backend_only` option for focused native backend checks.

The native recipe observation service is now a reusable fixture-only host module:
[recipe-generation evidence](../../audit/migration/rust-recipe-generation.json).
It checks retained ancestor/file identity and all raw consulted bytes, including
same-size/restored-timestamp changes. Ubuntu passes 148 backend tests/31 process
checks; Windows passes 144/23. It adds no live GUI Compile or package authority;
active ABA and final check/use races remain explicit qualification limits.
