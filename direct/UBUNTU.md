# Ubuntu setup and platform behavior

The desktop detects Windows, Ubuntu, and other Linux distributions. Windows
continues to use the bundled runtime and Windows launchers. Linux uses a native
virtual environment; copied Windows executables and package stores are never
used for native Linux builds.

## Install on Ubuntu

Use a normal desktop account and a writable checkout. Python 3.10 or newer,
four CPU cores, and enough space for native board toolchains are
required. Ubuntu 22.04 and 24.04 are the compatibility workflow targets.

Install the desktop dependencies once:

```bash
sudo apt update
sudo apt install python3-venv python3-tk libegl1 libgl1 libnss3 \
  libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 \
  libxcb-keysyms1 libxcb-render-util0 libxcb-xinerama0 xdg-utils
```

Qt WebEngine also needs ALSA: install `libasound2` on Ubuntu 22.04, or
`libasound2t64` on Ubuntu 24.04. See Qt's
[Linux requirements](https://doc.qt.io/qtforpython-6/overviews/qtdoc-linux-requirements.html)
for other distributions or missing system libraries.

From the project directory:

```bash
python3 direct/setup_ubuntu.py
bash direct/runThisOnUbuntu.sh
# Optionally open an existing sketch:
bash direct/runThisOnUbuntu.sh --project "/home/you/Arduino/MySketch"
```

Setup creates `.venv-linux`, installs bounded major dependency versions, and
checks imports. Rerun setup to repair missing dependencies. It preserves the
environment and never installs pip packages into system Python. Run the GUI as
your desktop account; do not disable Chromium's sandbox or run it through sudo.
Keep the local offline `src/editor` assets when copying the application.

## Boards, frameworks, and native uploads

Open the board picker and click **Refresh boards** to query PlatformIO's
[canonical board catalog](https://docs.platformio.org/en/stable/core/userguide/cmd_boards.html).
Installed manifests and the local catalog cache remain usable offline. Select
the exact model and one of its declared frameworks. An Arduino `.ino` project
requires Arduino; other frameworks require suitable C/C++ sources and libraries.
Unresolved or ambiguous Arduino definitions block compilation with an
explanation instead of substituting another board.

Linux builds and uploads use native PlatformIO packages under
`${XDG_DATA_HOME:-$HOME/.local/share}/mcu-flasher/platformio/<architecture>`.
The first build may download packages. The default upload transport belongs to
the board's PlatformIO definition; serial boards require a selected port,
while USB/debug programmers can use their native transport. Additional custom
platforms, programmers, and unusual board options still need the appropriate
PlatformIO definition and configuration. New boards can be discovered without
adding a hardcoded family to the app, but arbitrary future hardware cannot be
guaranteed compatible.

Windows retains its existing optimized ESP32, ESP8266 and AVR upload paths;
other platforms use native PlatformIO upload. A changed source or framework
always invalidates firmware reuse, even if **Skip Compile** was checked.

## Serial access

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
The optional AI panel runs `opencode` from PATH. Install and configure that CLI
separately; sessions do not restart or replay commands after failure. Windows
keeps its existing PowerShell/CMD terminal and assistant integration.

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
available RAM, including saved manual job counts. Fewer than four physical CPU
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

Serial reader failures retry at bounded delays, retain port selection, and
stop after repeated failures. Stale readers cannot disconnect newer sessions.
Permission failures need user correction. Editor renderer failures preserve
dirty source buffers in process memory and reload a bounded number of times;
a visible reload control remains available after that budget is exhausted.
This does not recover from complete process termination. Flashing, erasing,
resets, and shell commands are never automatically replayed by these recovery
paths. Errors remain visible; broad try/catch blocks do not prove recovery.

## Verification and current limits

```bash
.venv-linux/bin/python -B direct/verify_runtime.py \
  --render-dir temp/audit/glass-redesign
.venv-linux/bin/python -B direct/verify_performance.py
QT_QPA_PLATFORM=xcb .venv-linux/bin/python -B direct/verify_controls.py \
  --render-dir temp/audit/controls
QT_QPA_PLATFORM=xcb .venv-linux/bin/python -B direct/verify_browser_loading.py
QT_QPA_PLATFORM=xcb QT_SCALE_FACTOR=1.5 .venv-linux/bin/python -B direct/verify_responsive.py \
  --render-dir temp/audit/responsive
```

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
The controls verifier uses native Tk package-browser fixtures and isolated Qt
setup classes to check Actions, Settings, reset guards and all three palettes.
It never runs setup installation or writes live preferences. Use a desktop or
`xvfb-run -a env QT_QPA_PLATFORM=xcb` for Tk screenshots; headless Tk checks
otherwise skip. The setup dialog is rendered for UI verification only: Ubuntu
launches through its native environment, without the Windows bootstrap pipeline.
The responsive verifier also exercises small work areas, negative monitor
origins, compact control rows, popup scrolling and native Tk font scaling.
Use separate processes at `QT_SCALE_FACTOR=1`, `1.25`, `1.5` and `2`.

During this implementation the verifier and desktop renders passed on Windows.
Native Ubuntu and physical microcontrollers were unavailable locally. CI and
real board compile/upload checks remain necessary before calling an Ubuntu
release or a particular hardware target verified. Windows-only driver installers,
UNC mappings, and native launcher executables have no Linux equivalent.
