# Ubuntu setup and platform behavior

The desktop detects Windows, Ubuntu, and other Linux distributions. Windows
continues to use the bundled runtime and Windows launchers. Linux uses a native
virtual environment; copied Windows executables and package stores are never
used for native Linux builds. Ubuntu launch/setup files are in `direct/ubuntu/`;
Windows launch files are in `direct/windows/`. The shared GUI selects only one
host implementation through `main/core/toolchain.py`:

| File | Responsibility |
| --- | --- |
| `main/platforms/ubuntu.py` | Native PlatformIO, Linux package paths and POSIX build/upload processes |
| `main/platforms/windows.py` | Windows executable discovery, guarded package commands and short-path aliases |
| `main/core/build_resources.py` | Shared CPU, RAM and storage budgets |
| `direct/ubuntu/requirements.txt` | Native Linux dependencies, without Windows terminal/WebView packages |
| `MCU_Flasher` / `src/launcher_ubuntu.c` | Native amd64 executable and its rebuildable Linux-only source |
| `direct/ubuntu/launch.py` / `preflight.py` | First-run Bootstrap handoff, desktop launch and dependency diagnostics |
| `direct/ubuntu/install_desktop.py` | Local desktop shortcut and optional per-user Applications entry |

## Install on Ubuntu

Use a normal desktop account and a writable checkout on 64-bit Intel/AMD Ubuntu.
Python 3.10 or newer,
four CPU cores, and enough space for native board toolchains are
required. Ubuntu 22.04, 24.04 and 26.04 are the compatibility workflow targets.
The first-launch job uses each release's system Python, including Python 3.14
on 26.04. Native 26.04 compatibility still requires a completed CI run; the
existing local native verification covers 24.04.

Open `MCU_Flasher`, or run `./MCU_Flasher`. Bootstrap detects missing desktop
dependencies, opens a setup terminal and asks for Ubuntu administrator
authentication. It refreshes package indexes and installs the detected packages,
then prepares the private runtime and opens the app automatically. You do not
need to type apt commands. Internet access and an administrator account are
needed when packages are missing.

For manual provisioning, the equivalent desktop dependencies are:

```bash
sudo apt update
sudo apt install python3-venv python3-tk build-essential git libegl1 libgl1 libnss3 \
  libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 \
  libxcb-keysyms1 libxcb-render-util0 libxcb-xinerama0 libxcb-xkb1 \
  libxcb-shape0 libxcb-randr0 libxcb-sync1 libxcb-shm0 libxcb-xfixes0 \
  libxcb-render0 libxcb-util1 libxcb1 libxcb-dri3-0 libgbm1 libnspr4 \
  libx11-6 libx11-xcb1 libxext6 libxcomposite1 libxdamage1 libxfixes3 \
  libxrandr2 libxrender1 libxtst6 libxkbfile1 libxkbcommon0 \
  libdbus-1-3 libfontconfig1 libfreetype6 libusb-1.0-0 libudev1 xdg-utils \
  ripgrep xclip wl-clipboard libsecret-tools
```

Qt also needs ALSA and GLib: install `libasound2` and `libglib2.0-0` on Ubuntu
22.04, or `libasound2t64` and `libglib2.0-0t64` on Ubuntu 24.04 and 26.04.
Bootstrap selects these names automatically. See Qt's
[Linux requirements](https://doc.qt.io/qtforpython-6/overviews/qtdoc-linux-requirements.html)
for other distributions or missing system libraries.

Ubuntu 26.04's GNOME desktop uses Wayland and retains XWayland for X11 apps.
Bootstrap accepts either desktop display and prepares both clipboard backends.
The CI renderer checks currently use X11 through Xvfb; they do not prove native
Wayland behavior. See the [Ubuntu 26.04 release notes](https://documentation.ubuntu.com/release-notes/26.04/summary-for-lts-users/#wayland-session).

From the project directory:

```bash
# Launch the supplied native executable, or double-click it in Files:
./MCU_Flasher
# Check prerequisites/readiness without setup or GUI:
./MCU_Flasher --check
# Register an Applications menu entry and generate the local desktop shortcut:
./MCU_Flasher --install-shortcut
# Rebuild the executable/shortcut from source when needed:
bash direct/ubuntu/build_launcher.sh
# The original explicit setup and terminal launch still work:
python3 direct/ubuntu/setup.py
bash direct/ubuntu/run.sh
# Optionally open an existing sketch:
bash direct/ubuntu/run.sh --project "/home/you/Arduino/MySketch"
# Open another sketch in an independent window:
bash direct/ubuntu/run.sh --project "/home/you/Arduino/AnotherSketch" --new-window
# Repair runtime dependencies explicitly:
bash direct/ubuntu/run.sh --repair
```

`MCU_Flasher` is a native ELF launcher for this application folder, like the
existing Windows launcher. Keep it beside `direct/`, `main/` and `src/`; it is
not a single-file bundle of Python and board toolchains. Executable permissions
are supplied; if a copy loses them, use `chmod +x MCU_Flasher`. A filesystem
mounted with `noexec` can use `bash direct/ubuntu/run.sh` to enter setup, but the
private runtime and native tools still need executable storage. The generated
folder shortcut resolves the application beside the shortcut at click time and
invokes Bash, so its launch path survives a folder move and a lost ELF executable
bit. Keep that shortcut beside `direct/`, `main/` and `src/`. Ubuntu may require
right-clicking it and choosing **Allow Launching**. The optional Applications menu
entry uses absolute paths; after moving the folder, run
`bash direct/ubuntu/run.sh --install-shortcut` to regenerate that menu entry.

## Copied folders and launch problems

An older `MCU Flasher.desktop` may still point to another machine's `/home/...`
folder. Clicking it fails before Bootstrap can run. From the current application
folder, use:

```bash
bash direct/ubuntu/run.sh --check
bash direct/ubuntu/run.sh --install-shortcut
bash direct/ubuntu/run.sh
```

The first command only diagnoses dependencies and runtime readiness. The second
replaces the local shortcut and registers the current folder in Applications.
New local shortcuts resolve their own location, including local `file:` URIs,
and pass paths and Repair arguments literally. A fixed Bash dispatcher locates
the source entry without requiring Python first, preserving Bootstrap's
missing-Python recovery. The workspace still uses `.venv-linux`.

A strange folder such as `_`, containing
`home/.../temp/audit/bootstrap-seed-fixtures`, is residue from older verification
code. A shared helper added Windows's `\\?\` prefix to a Linux absolute path;
Linux treated that prefix as a relative directory name. Copying or extraction
can turn the backslashes into private-use Unicode glyphs. The helper now adds
the prefix only on Windows, and verification checks extraction containment.
The residue is not required to launch the app. Inspect its contents before
removing anything; preserve unrecognized user files and protected caches.

Use the Linux `MCU_Flasher` or Bash entry. The `.exe`, bundled Windows Python and
copied Windows board tools cannot serve as Ubuntu's runtime. A writable native
Linux filesystem with execution enabled is recommended for the application and
its runtime; a `noexec` mount can also prevent `.venv-linux/bin/python` and
compiler tools from starting.

If the workspace opens but its editor stays blank and the launch log reports
`GBM is not supported` or `Compositor returned null texture`, check the graphics
driver separately. For one diagnostic launch, use Qt WebEngine's documented
[software rendering fallback](https://doc.qt.io/qt-6.10/qtwebengine-features.html#hardware-acceleration):

```bash
env QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu bash direct/ubuntu/run.sh
```

This applies only to that launch. A GPU warning alone does not establish a
startup failure; confirm the editor's behavior on the Ubuntu desktop.

## Bootstrap preparation

First launch checks Ubuntu's Tk, native libraries and build tools before any
dependency install. Missing OS packages such as `libxcb-cursor0` and
`libxcb-xinerama0` enter Bootstrap automatically. Only Ubuntu's `apt-get` runs
with administrator rights through `sudo` in the setup terminal, or `pkexec`
when a graphical authentication agent is available. Setup Python and the GUI
always run as your desktop account. Bootstrap installs only detected known
prerequisites and their dependencies; package removal is refused. See Ubuntu's
[apt-get documentation](https://manpages.ubuntu.com/manpages/noble/man8/apt-get.8.html)
for package-manager behavior.

Cancelled authentication, apt errors or a failed dependency recheck stop setup
before the private runtime or GUI starts. The error stays visible; launch again
after resolving it. No automatic authentication retry or lock deletion occurs.
If system Python itself is missing, the shell launcher installs `python3` and
`python3-venv` first, then continues the same flow. `--check` always reports
readiness without authentication or installation.

When system packages, Python dependencies or readiness need repair, a desktop
launch opens the available Ubuntu terminal with live Bootstrap output. Failed
setup stays visible; success opens the workspace and closes that setup terminal.
Healthy launches need no terminal or administrator prompt.
Immediate desktop launch failures include a log under `logs/ubuntu-launch-*.log`.

Setup creates `.venv-linux`, installs bounded dependency versions and checks
private imports, Qt xcb/WebEngine linkage and the separate Qt5 library viewer.
First launch and `--repair` prepare the application runtime, SCons and the
configured native board/framework/tool/library packs in `direct/offline-packages.json`:
AVR, ESP32 and ESP8266 with Arduino and the listed libraries. Ubuntu needs its
own platform definitions and native compilers even when Offline Mode is off.
The launcher requires the preparation certificate, so an older SCons-only setup
automatically enters Bootstrap again. Existing certified packages are reused.
This preparation leaves the selected Online/Offline Mode unchanged; explicit
`--plan`/`--board-source` can customize the configured preparation. Rerun setup to
repair missing dependencies.
The Ubuntu workspace reads that saved package mode before activating its
installation guard. Offline Mode applies only to board/library preparation;
Cloud sketches and Developer Access each check their own internet connection.
Application networking remains available in both modes, while package
installation remains Bootstrap-only.
Ubuntu omits the reviewed, unused Windows-only ESP32 `tool-mconf` package from
preparation. Required tools, custom package sources and Windows plans remain
unchanged; Bootstrap still stops on genuine package preparation failures.
It preserves installed packages, repairs stale copied interpreter links, refuses
external environment symlinks and never installs pip packages into system Python.
GUI bindings use binary wheels to avoid an unattended Qt source build. Their
joint PySide6/PyQt5/QScintilla wheel availability currently limits this release
to amd64; ARM is rejected with an explanation. Run the GUI as
your desktop account; do not disable Chromium's sandbox or run it through sudo.
Keep the local offline `src/editor` assets when copying the application. The old
`direct/setup_ubuntu.py`, `runThisOnUbuntu.sh` and `requirements-ubuntu.txt`
paths forward to the Ubuntu files for compatibility.

## Boards, frameworks, and native uploads

Open the board picker and click **Refresh boards** to rescan locally prepared
definitions. Bootstrap saves PlatformIO's canonical catalog before offline use.
Installed manifests and the local catalog cache remain usable offline. Select
the exact model and one of its declared frameworks. An Arduino `.ino` project
requires Arduino; other frameworks require suitable C/C++ sources and libraries.
Unresolved or ambiguous Arduino definitions block compilation with an
explanation instead of substituting another board.
Bootstrap writes a complete per-declaration board coverage report in the native
package store (`.mcu-bootstrap-board-coverage.json`). For generic Arduino entries
without a unique PlatformIO equivalent, Bootstrap can prepare the original exact
Arduino core/version/FQBN from verified source metadata. Bootstrap verifies an
installed native Arduino CLI or prepares its pinned native release first.
Source bytes and a real preparation compile are
verified before runtime uses this backend; the generic declaration keeps its
default pin and memory settings. Missing metadata/tools remain unavailable with
an explicit reason, rather than selecting another physical model automatically.
Prepared original Arduino targets keep separate stores for exact core versions
and package indexes. Their builds also use the libraries prepared by the package
plan and Library Downloader, with byte checks before firmware reuse.

Linux builds and uploads use native PlatformIO packages under
`${XDG_DATA_HOME:-$HOME/.local/share}/mcu-flasher/platformio/<architecture>`.
Build preparation replaces inherited Windows package/cache/interpreter hints
with native Linux locations; it never trusts a copied Windows readiness marker.
Launch, Bootstrap and build preparation clear PlatformIO interpreter overrides
(`PYTHONEXEPATH`, `PIO_PYTHON_EXE`, `PLATFORMIO_PYTHON_EXE` and
`PLATFORMIO_PENV_DIR`). XDG data/cache roots must be absolute native Linux paths;
copied Windows or relative values use the account's default folders instead.
The main app and its build/upload/reset subprocesses prohibit downloads. Missing
packages report the bootstrap command. Edit `direct/offline-packages.json` to
add a custom platform or a registry library specification, then run setup while
online. The default plan includes AVR, ESP32 and ESP8266 with Arduino, their
upload/debug package variants, and Servo, ESP32Servo and ArduinoJson.
An arbitrary sketch may need additional libraries in that plan. Initial setup
can take substantial time and storage; subsequent launches need no network
probe. To use a separate plan, run `python3 direct/ubuntu/setup.py --plan "/path/to/plan.json"`.
Setup certifies readiness only after every preparation step succeeds; deleting
a prepared package or changing the default plan requires setup again.
The default upload transport belongs to
the board's PlatformIO definition; serial boards require a selected port,
while USB/debug programmers can use their native transport. Additional custom
platforms, programmers, and unusual board options still need the appropriate
PlatformIO definition and configuration. New boards can be discovered without
adding a hardcoded family to the app, but arbitrary future hardware cannot be
guaranteed compatible.

Windows retains its existing optimized ESP32, ESP8266 and AVR upload paths;
other platforms use native PlatformIO upload. A changed source or framework
always invalidates firmware reuse, even if **Skip Compile** was checked.
Native upload output shares the same bounded reader and routine-scan filter as
Windows uploads. Dependency graphs and build-scan chatter stay hidden while
programmer messages, warnings, errors and PlatformIO's final result remain
visible. Quiet upload phases update one status line instead of
repeatedly filling the console. Stop remains available during scans and
connection attempts, then disables when programmer output first indicates an
erase/write. A failed write is not replayed or followed by an automatic reset.

## Serial access

The Ubuntu **Serial port** list omits unidentified entries with blank or `N/A`
details. Real device descriptions remain visible; when a description is missing,
the list uses available product/manufacturer information or a hardware ID.
A bare tty name or symlink path does not count as device details. Enumeration
never opens ports to decide what to show, and Windows discovery is unchanged.

Select `/dev/ttyUSB*` or `/dev/ttyACM*` in **Serial port**. For permission errors,
check the device group, then add your account to Ubuntu's serial group:

```bash
sudo usermod -aG dialout "$USER"
```

Sign out and back in. USB/debug probes may also require the official
[PlatformIO udev rules](https://docs.platformio.org/en/stable/core/installation/udev-rules.html).
The application reports permission errors and does not attempt privilege
escalation. Disconnect other applications holding the port.

## Terminal, assistant, and recovery

Linux's **Terminal** tab uses a native Bash PTY with the bundled xterm frontend.
Input writes are nonblocking and retain partial UTF-8/paste bytes through PTY
backpressure. Queued input is bounded to 4 MiB; an over-capacity input is rejected
with a visible notice. Closing a session clears pending input without replay.
The **AI Assistant** pane uses `main/qt/posix_ai_panel.py` to render a dedicated
OpenCode TUI with one protected native PTY. **Hide** and reveal keep the same
session running; an independent close of its container is ignored. Closing the
owning workspace ends it. The separate **Terminal** tab remains empty until
**New Bash** creates a general shell session.
If a project-terminal renderer stops or its assets fail to load, only that
session's PTY closes. Its tab remains available, and **New Bash** starts a new
session explicitly; commands are never replayed. The assistant header fits
compact panes using the current font's logical dimensions.

Ubuntu creates PTY pairs with `openpty` and starts the private supervisor through
native `Popen` session setup. Python does not run a fork callback inside the
threaded Qt workspace. The fresh supervisor acquires the controlling terminal
and foreground process group before starting the command, avoiding Python
3.12's deprecated threaded `forkpty` path.

Each Ubuntu PTY command runs under that private supervisor, which keeps ownership
of tools that create separate process sessions. When the command exits, those
tools are stopped and reaped before the PTY ends or a fresh assistant starts.
Keyboard interrupts and Bash job control still reach the command normally;
cleanup runs outside Qt and does not replay input.
The native PTY close API preserves descriptor-close exceptions after the owned
supervisor is reaped; it no longer suppresses them by returning from `finally`.

`/exit` starts a fresh assistant PTY without replaying input. Unexpected exits
allow at most two automatic restarts within one minute, then leave a visible
**Retry OpenCode** control. `main/platforms/ubuntu_opencode.py` discovers the
certified `.ubuntu-tools/opencode` executable first, then native OpenCode through
the desktop account's PATH and standard user installation
directories: `~/.opencode/bin`, `~/.local/bin`, `~/.npm-global/bin` and
`~/.bun/bin`. Bootstrap verifies an installed CLI or prepares a pinned release;
configure the provider and model in OpenCode itself. Runtime discovery does not
download packages or run a shell. Windows keeps its existing PowerShell/CMD
terminal and assistant integration.

Both project terminal implementations preserve terminal capability replies,
Unicode input, alternate screens and bracketed paste for installed coding
CLIs. Output is acknowledged after xterm parses it, so a slow renderer applies
backpressure instead of accumulating unlimited output. Clear does not type
`cls` or `Clear-Host` into the current CLI. On Windows the hardware-free
`direct/verify_terminal.py` probe checks the browser/PTY protocol and runs only
installed CLI version commands; authenticated coding sessions are not run.

Devices with 4 or 6 physical cores, or at most 6 logical CPU threads,
automatically use shorter terminal
scrollback, less editor animation, coalesced rendering and less frequent
background syntax checks. Compilation reserves two CPU threads and respects
available RAM, including saved manual job counts. On four/six physical cores,
compiler concurrency is capped at two/four jobs even with SMT; low available
RAM or mechanical storage can reduce it further. The shared editor sends each
dirty recovery snapshot once, reuses it for syntax checks, and debounces typing
checks for 600 ms on constrained PCs. Unchanged diagnostic tables retain their
rows and reading state. Fewer than four physical CPU
cores prevents startup with a compatibility notice. If physical topology cannot
be reported, the check falls back to logical threads. Logical CPU detection failure
also stops startup. A desktop Ubuntu session uses a Tk notice when available,
and headless sessions print the same notice to stderr.

The shared interface uses translucent gradients, rounded surfaces, vector
icons, and opaque reading panes. It does not depend on desktop compositor blur.
Smoked dark and frosted light themes share the same layout and focus controls.
Board and port selection stay in Controls, with no target-board banner. Source
and tool tabs have distinct selected/focus states and compact labels. Source tabs
use exact filenames and preserve Linux path case; keyboard navigation and drag
ordering retain the correct model. Only explicit Detach Editor opens a separate
editor window, and closing it restores the same editor and dirty buffers.

Ubuntu pointer recovery and log mouse-release listeners use constructed widget
filters. They avoid application-wide Python filters that can recurse while
Shiboken wraps private Qt/WebEngine objects. Native Build/Serial Clear also keeps
retained text cursors alive until Qt finishes clearing its document, avoiding a
cursor use-after-free during synchronous scrollbar callbacks. Both fixes are
Linux-only; Windows retains its existing paths.

Serial reader failures retry at bounded delays, retain port selection, and
stop after repeated failures. Stale readers cannot disconnect newer sessions.
Permission failures need user correction. Editor renderer failures preserve
dirty source buffers in process memory and reload a bounded number of times;
a visible reload control remains available after that budget is exhausted.
This does not recover from complete process termination. Flashing, erasing,
resets, and shell commands are never automatically replayed by these recovery
paths. Errors remain visible; broad try/catch blocks do not prove recovery.

## Verification and current limits

The Ubuntu parity audit additionally covers Bootstrap's native Arduino CLI and
OpenCode preparation, clipboard/search tools, private esptool reset commands,
Download Manager reuse and shutdown, assistant keyboard ownership, current
project context, background prompt history, tool descendants and renderer
failure. Windows paths remain separate. Reopening Boards & Libraries wakes the
existing Ubuntu manager; closing one project leaves it available to other open
projects from the same application checkout.

Bootstrap reuses verified installed CLIs or prepares pinned native releases in
`.ubuntu-tools/`, without changing external installations or assistant account
configuration. Healthy runtime discovery does not download packages. Older
installations missing these tools return to Bootstrap automatically on launch.
The assistant's provider sign-in and model setup stay in OpenCode itself.

The private reset tool uses current esptool v5 command names and the exact-port
chip connector, retaining esptool v4 syntax when that version is prepared.
Windows reset command paths are preserved.

Hardware-free regressions, using the application's private Python:

```bash
.venv-linux/bin/python -B direct/verify_ubuntu_arduino_cli.py
.venv-linux/bin/python -B direct/verify_ubuntu_opencode_setup.py
.venv-linux/bin/python -B direct/verify_ubuntu_download_manager.py
.venv-linux/bin/python -B direct/verify_ubuntu_resets.py
.venv-linux/bin/python -B direct/verify_ubuntu_pty_lifecycle.py
xvfb-run -a env QT_QPA_PLATFORM=xcb .venv-linux/bin/python -B \
  direct/verify_ubuntu_ai_panel.py --workspace-renderer
```

The workspace probe uses a local fixture CLI with the real Qt/PTY and Monaco
workspace. It checks key delivery, hide/reveal, detach/reattach focus, fonts,
palettes, fresh `/exit` and shutdown without AI requests or hardware access.
Actual reset-tool verification uses local firmware-image bytes and `--help`,
without opening a serial port. These checks do not prove physical flashing,
an authenticated assistant conversation or a real Wayland compositor session.

To exercise the complete first launch, including real downloads and private
runtime creation, use this opt-in integration check after installing the system
prerequisites:

```bash
xvfb-run -a /usr/bin/python3 -B direct/verify_ubuntu_first_run.py --execute
```

It copies application sources/assets into a new `temp/audit/ubuntu-first-run/`
fixture with an empty runtime and native store, runs the native launcher and the
full default Bootstrap, opens the actual Monaco workspace, verifies a prepared
launch, clicks Compile for ESP32 and runs the runtime checks. It redirects home
paths in the fixture and prohibits serial opens; installers run normally only
inside the fixture. It leaves logs and firmware there and never installs system
packages. CI runs this separately on Ubuntu 22.04/24.04/26.04; its explicit
`--allow-small-runner` override affects only the disposable copy's CPU admission.

```bash
.venv-linux/bin/python -B direct/verify_ubuntu_launcher.py
.venv-linux/bin/python -B direct/verify_ubuntu_bootstrap.py
.venv-linux/bin/python -B direct/verify_ubuntu_runtime.py
.venv-linux/bin/python -B direct/verify_ubuntu_opencode.py
.venv-linux/bin/python -B direct/verify_ubuntu_ai_panel.py
xvfb-run -a env QT_QPA_PLATFORM=xcb .venv-linux/bin/python -B direct/verify_ubuntu_ai_panel.py --native-renderer
.venv-linux/bin/python -B direct/verify_ubuntu_event_filters.py
.venv-linux/bin/python -B direct/verify_platforms.py
.venv-linux/bin/python -B direct/verify_offline.py
.venv-linux/bin/python -B direct/verify_target_resolution.py
.venv-linux/bin/python -B direct/verify_board_families.py
.venv-linux/bin/python -B direct/verify_board_search.py
.venv-linux/bin/python -B direct/verify_projects.py
.venv-linux/bin/python -B direct/verify_application_guard.py
.venv-linux/bin/python -B direct/verify_actions.py
.venv-linux/bin/python -B direct/verify_cursor.py
.venv-linux/bin/python -B direct/verify_preferences.py
.venv-linux/bin/python -B direct/verify_storage_resources.py
.venv-linux/bin/python -B direct/verify_storage_io.py
.venv-linux/bin/python -B direct/verify_slow_storage_flow.py
.venv-linux/bin/python -B direct/verify_catalog_io.py
.venv-linux/bin/python -B direct/verify_upload_workers.py
.venv-linux/bin/python -B direct/verify_build_log_routing.py
QT_QPA_PLATFORM=xcb .venv-linux/bin/python -B direct/verify_serial_view.py
node direct/verify_editor_preferences.js
.venv-linux/bin/python -B direct/verify_runtime.py \
  --render-dir temp/audit/glass-redesign
.venv-linux/bin/python -B direct/verify_performance.py
QT_QPA_PLATFORM=xcb .venv-linux/bin/python -B direct/verify_controls.py \
  --render-dir temp/audit/controls
QT_QPA_PLATFORM=xcb .venv-linux/bin/python -B direct/verify_browser_loading.py
QT_QPA_PLATFORM=xcb QT_SCALE_FACTOR=1.5 .venv-linux/bin/python -B direct/verify_responsive.py \
  --render-dir temp/audit/responsive
```

The Ubuntu assistant checks isolate discovery and session lifecycle. The
`--native-renderer` probe additionally uses real Qt WebEngine/xterm.js with a
local fake CLI over a real PTY. It performs no authentication, AI requests,
hardware access or live cache writes; fixtures and captures stay in `temp/`.

The hardware-free verifier checks target validation, cache identity, recovery
budgets, serial reader ownership, Qt action gating, editor snapshots, framework
selection, compact/wide widget ownership, source-tab keyboard navigation,
detach/reattach and native Linux PTY/lock behavior. It uses simulated sketch data and
never opens a real serial port, builds firmware, or flashes hardware.
The performance verifier checks bounded display/event queues, newline-free
streams, serial Clear, latest-revision parsing, GUI-thread completion and local
warm-launch health helpers with temporary fixtures. The Windows bootstrap fast
path is Windows-only; Ubuntu continues using its native virtual environment.
`.github/workflows/compatibility.yml` runs that verifier on Windows and Ubuntu.
The workflow also runs the recent pointer/focus, saved-preference, Documents
browse, incremental catalog, storage I/O and upload-worker regression fixtures
on both Ubuntu versions. Node 22 runs the real editor-preference script; the
Ubuntu runtime/editor render check uses an Xvfb desktop with the native Qt xcb
backend. Local Windows simulations are not native Ubuntu execution proof.
The platform verifier checks isolated host selection, import order, native
package paths, setup/launch arguments and build/upload process options. It
parses Bash scripts without launching; the native Windows Script Host check
skips on Ubuntu. Setup and hardware calls are mocked.

The Ubuntu CI job additionally provisions native AVR packages under `temp/`,
then uses copied packages to compile Uno, Nano and Mega through the application's
Compile button. To run that optional integration locally with already installed
native packages, use `.venv-linux/bin/python -B direct/verify_target_resolution.py
--compile-installed-avr`. An explicit `--avr-core /path/to/native/store` is also
accepted. The probe refuses missing packages and performs no install or upload.
The controls verifier uses native Tk package-browser fixtures and isolated Qt
setup classes to check Actions, Settings, reset guards and all three palettes.
It never runs setup installation or writes live preferences. Use a desktop or
`xvfb-run -a env QT_QPA_PLATFORM=xcb` for Tk screenshots; headless Tk checks
otherwise skip. The setup dialog is rendered for UI verification only: Ubuntu
launches through its native environment, without the Windows bootstrap pipeline.
The responsive verifier also exercises small work areas, negative monitor
origins, compact control rows, popup scrolling and native Tk font scaling.
Use separate processes at `QT_SCALE_FACTOR=1`, `1.25`, `1.5` and `2`.

Local Ubuntu 24.04 amd64 verification passes with the native xcb backend on an
isolated Xvfb display: 35 runtime checks plus the real Monaco workspace, seven
event-filter/heap-crash checks, 25 native serial-view checks, 24 build-console
checks, and 26 performance checks. The responsive suite passed 14 Qt/Tk checks
at 100/125/150/200% with PySide6 6.8.3 and again at 100% with 6.12; current
runtime/renderer/crash checks also passed with 6.12. Existing Qt dependency ranges
remain unchanged. The clipboard verifier needs a native xcb display through
process exit; the offscreen plugin can crash while releasing transferred MIME
data after its assertions pass. Ubuntu CI runs that fixture under Xvfb.

The early verification host lacked `libxcb-cursor0` and `libxcb-xinerama0`; validation supplied
official Ubuntu libraries only in temporary process-scoped fixtures. Normal
launch now hands those packages to automatic Bootstrap with administrator
authentication. The isolated system-setup suite verifies successful installation,
cancellation, apt failures, dependency rechecks, private-runtime continuation
and missing-Python rescue with fake installers. Those UI/system checks did not
run installers or firmware operations; separate isolated native first-run and
ESP32 Compile probes are described below. Ubuntu 22.04,
Wayland-specific desktop behavior and actual hardware remain separate checks.
Windows driver installers and UNC mappings remain Windows-specific; the new
`MCU_Flasher` supplies Ubuntu's native executable launcher.

The missing **ESP32 Dev Module** definition was traced to Ubuntu's earlier
SCons-only online setup. First-run/repair now prepares the configured native
packs, and launcher readiness rejects that incomplete legacy setup. A real
native xcb Compile-button test under `temp/audit/ubuntu-esp32` repaired a stale
Arduino selection to `espressif32:esp32dev`, converted its isolated `.ino`, ran
the Linux compiler through the application worker and produced `firmware.bin`.
Package-store coordination, settings and notifications were isolated; no USB
upload or live sketch changes were performed. Run the same check against an
explicitly provisioned fixture with:

```bash
.venv-linux/bin/python -B direct/verify_target_resolution.py \
  --compile-native-esp32 temp/audit/ubuntu-esp32/core
```
