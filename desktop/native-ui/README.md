# Native Rust widget feasibility fixture

This is an isolated native-widget prototype for the requested Rust GUI. It is
separate from the existing Tauri web-shell fixture and the production Python
application. It does not change either entry point.

The Windows adapter now constructs real Win32 controls and two child WebView2
islands. It preserves the native owner and moves only the retained editor view.
The Linux adapter retains GTK3 controls and WebKitGTK child views. Windows
cross-target strict Clippy passes. Actual Windows Server and Ubuntu AMD64/ARM64
execution now pass 17 native smoke checks per host at `77e5ccb`, in migration
CI run 39357769519. These are hosted fixture receipts; Windows 10/11 desktop
input, IME, mixed-monitor DPI and physical device workflows remain unqualified.

The window, Controls label, fixture-board combo box, Compile fixture button,
status label, splitter and detach action are real GTK3 widgets constructed by
Rust. Two independent embedded Wry/WebKitGTK views contain the retained offline
Monaco editor and xterm terminal. There is no HTML application toolbar. The
application callbacks and bounded renderer-report handling are Rust; Python is
used only by the developer smoke controller. GTK and the system browser engine
remain native dependencies, and the two embedded components remain JavaScript.
This is not a claim that every dependency or native library is implemented in
Rust.

The board names are explicitly fictional fixture values. **Compile fixture
exercises a native Rust callback; it does not compile firmware.** Current Python
application board compilation qualification is a separate campaign. The full
application backend still requires migration and parity qualification before
Python application modules can be retired.

## Isolation and resource limits

- All web resources are fixed, compile-time copies of existing repository
  assets. The original Monaco/xterm files are unchanged. A finite custom-origin
  asset allowlist exposes no filesystem path or network proxy.
- CSP blocks remote content, frames, forms and object embedding. Navigation,
  new windows and downloads are refused. Only the native Detach editor action
  opens an additional window.
- Typed IPC accepts reports from the native-bound editor or terminal island.
  It refuses unknown fields, renderer-supplied ownership, the wrong island and
  messages over 16 KiB. It offers no file, process, serial or settings command.
- The original native main window remains the fixture owner while only the
  editor view moves. No renderer-supplied owner identifier is accepted. This is
  a UI fixture, not qualification of live project ownership or source writes.
- WebKit profiles and process-local XDG data/config/cache folders are created
  atomically under `audit/migration/scratch/` and only that owned profile is
  removed on normal exit. No user HOME, shell profile, application settings,
  sketch or protected build cache is changed.
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
```

The controller creates its own private Xvfb display and D-Bus session. It writes
`audit/migration/scratch/native-ui/latest-smoke.json` and `latest-output.log`.
This is native X11 functional execution using software rendering; it does not
qualify GPU performance or a GNOME desktop session. Use the existing display
only when explicitly verifying that display:

```bash
GDK_BACKEND=wayland .venv-linux/bin/python3 desktop/native-ui/verify-smoke.py --current-display
```

That command requires a real accessible Wayland session. No Wayland result has
been claimed from an Xvfb run. For manual fixture inspection, run
`desktop/target/debug/mcu-native-ui-probe` without `--smoke`.

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

The migration-prototype CI matrix runs seven Rust checks, five controller
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
with native Rust UI source unchanged. Focused CI for subsequent reader/release
selection checkpoint `8dbdb9d` remains pending. Each receipt retains its tested
source; no configured or pending job is counted as successful.

## Next gates

Keep `mcu-contracts`, `mcu-core` and `mcu-host` as reusable Rust service layers;
the Tauri adapter can remain a differential fixture. This UI probe intentionally
does not duplicate or activate their live-write capabilities.

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
   APIs, not keyboard routing or complete buffer/tab behavior.
4. Carry the existing compact glass palette, icons, saved fonts and auxiliary
   screens into the native shell, with full responsive and resource budgets.
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
