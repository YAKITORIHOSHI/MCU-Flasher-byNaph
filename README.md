# 🛠️ MCU Flasher by Naph

> **A glass desktop workspace for compiling, flashing, monitoring, and coding microcontrollers on Windows and Ubuntu.**

![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Ubuntu-blue)
![Architecture](https://img.shields.io/badge/architecture-PySide6%20%7C%20Qt%20for%20Python-41CD52)
![Python](https://img.shields.io/badge/python-3.10%2B%20(Private%20Runtime)-blue)
![Toolchain](https://img.shields.io/badge/toolchain-PlatformIO%20%2B%20Arduino%20CLI-orange)
![License](https://img.shields.io/badge/license-MIT-green)

---

## 📖 Table of Contents

- [✨ Features](#-features)
- [🚀 Quick Start](#-quick-start)
- [📁 Project Structure](#-project-structure)
- [📘 User Guide & How to Use](#-user-guide--how-to-use)
  - [1. Launching & First-Run Auto-Bootstrap](#1-launching--first-run-auto-bootstrap)
  - [2. Opening, Selecting & Scaffolding Projects](#2-opening-selecting--scaffolding-projects)
  - [3. Selecting Boards & COM Ports](#3-selecting-boards--com-ports)
  - [4. Compiling & Flashing Code](#4-compiling--flashing-code)
  - [5. Live Serial Monitor & Post-Upload Auto-Reset](#5-live-serial-monitor--post-upload-auto-reset)
  - [6. Critical Operation Protection & Safe Shutdown](#6-critical-operation-protection--safe-shutdown)
  - [7. Offline Monaco Code Editor](#7-offline-monaco-code-editor)
  - [8. Multi-Session Project Terminal](#8-multi-session-project-terminal)
  - [9. OpenCode AI Assistant & Pulsating Diff Glow](#9-opencode-ai-assistant--pulsating-diff-glow)
  - [10. Soft Reset & Hard Reset Recovery Flashing](#10-soft-reset--hard-reset-recovery-flashing)
  - [11. Remote Network Shares (UNC Paths)](#11-remote-network-shares-unc-paths)
- [🧭 Architectural Reference: What is What & Which is Which](#-architectural-reference-what-is-what--which-is-which)
  - [Root Entry Points & Launchers](#root-entry-points--launchers)
  - [The Native PySide6 (Qt) Desktop Architecture & Web Bridge Engine](#the-native-pyside6-qt-desktop-architecture--web-bridge-engine)
  - [The `main/core/` Foundation Modules](#the-maincore-foundation-modules)
  - [The `src/modules/` System Services & Runtime Guards](#the-srcmodules-system-services--runtime-guards)
  - [Offline Monaco Editor & Web Assets](#offline-monaco-editor--web-assets)
  - [Caches, Installers & Reset Templates](#caches-installers--reset-templates)
- [⚙️ Configuration](#️-configuration)
- [🛠️ Development & Contributing](#️-development--contributing)
- [📄 License](#-license)

---

## ✨ Features

The shared interface now uses translucent surfaces, subtle gradients, rounded
panels, portable vector icons, and responsive action menus. Board and serial
selection stay in the Controls row, with no target-board banner. Source and
tool tabs use individual glass surfaces with clear selection and keyboard focus.
The editor and every tool pane extend to the central workspace edges, while
controls and text retain their inner reading padding and splitters remain usable.
Source tabs retain dirty indicators and drag ordering; arrow keys, Home and End
switch files when the tab row has focus. Long filenames truncate with full-path
tooltips and horizontal scrolling.
Toolbar buttons, tab padding and workspace gaps are slightly smaller; saved
editor and terminal font sizes are preserved. Controls reflow into additional
rows as the window narrows. The Workspace heading stays aligned above its
action buttons or compact Options menu; Timestamps and Skip Compile stay together.
Settings fields stack above their inputs when
needed, board filters wrap, and short detail panes scroll. The downloader moves
its header actions below the title and stacks package lists above details.
On short screens, the editor/tool splitter reserves space for the selected
tool's header, search or send controls, and scrollable output. Tool navigation
stays visible while the editor takes the remaining space.
Narrow, short windows compact Controls into fewer rows; very short windows move
Timestamps and Skip Compile into **Options**. Serial Monitor moves
display options and Copy/Clear into **Options** when needed, keeping baud,
Reset, Pause and Send reachable. Syntax Check uses the chosen content font.
Qt sizing uses logical pixels and follows the window's current monitor and
available work area, including taskbars and negative monitor coordinates.
Qt applies display scaling once; Tk uses its own native font and pixel metrics.
Monitor and resize changes are coalesced without an idle polling loop.
Sketch projects keep app-generated build inputs and history in the hidden
`.mcu_flasher_build_cache` container. On Windows, generated root instructions,
ignore rules, proven MCU Flasher IDE metadata and first-use sentinels are hidden
too. Metadata updates preserve hidden attributes and replace files atomically;
user sources, documents and user-authored instructions/settings remain visible
and are not overwritten by instruction generation.
Each main window has a minimum width of half the current monitor's available
width. It can resize wider and maximize normally; saved maximized state is
restored. Minimum sizing follows the current monitor in Qt logical pixels.
Tabs map to exact root filenames rather than loading order, and Ubuntu paths
retain case. Startup project loading is coalesced to avoid duplicate models.

Startup opens the main workspace. The compact **Actions** menu belongs to its
toolbar; it does not create a separate desktop window. **Detach Editor** opens
the full code editor only on request. Closing it attaches the same editor and
its unsaved buffers back to the main workspace.
Settings reset and system-theme fallback resolve the current glass theme names.
The downloader shows cached catalogs before network refreshes and keeps compact
grouped metadata in an automatically rebuilt cache. Fresh local indexes need no
connectivity probe. Installed-package and detail scans run in bounded workers;
rapid selections cancel older scans. Unchanged inventories reuse a five-second
snapshot, and download progress updates are coalesced. Network concurrency is
limited to two board-index requests on constrained PCs and four otherwise.
Library samples open in a separate read-only viewer with the selected theme and
saved content font. Its tabs, syntax, selection, line numbers and scrollbars
follow Glass, Frosted Light and Solarized palettes. The complete window frame
fits the monitor's logical work area, including compact and portrait displays.
Interpreter probes run off the Tk thread with one active and one latest request;
superseded or closed-window results never open another viewer. Missing viewer
dependencies direct users to Bootstrap. Viewing samples never installs packages
or writes sample files/settings. Verify with `direct/verify_library_samples.py`
in a separate process from the PySide6 checks.
If a browser worker cannot start, it reports the failure on the Tk thread and
restores Download/Update controls and progress. Pending work is not replayed;
the next explicit click retries normally.
The editor engine is offline Monaco with automatic resource settings; Settings
does not offer the unused legacy lightweight-engine selector.

The board picker refreshes locally prepared PlatformIO definitions and preserves
installed/cache entries. Discovery computes matching evidence once per refresh
to avoid repeatedly normalizing each Arduino/PlatformIO pair. Matching scores,
ambiguity rejection and unavailable framework guards retain their exact rules;
a new refresh rebuilds the evidence from the current manifests. The picker
validates exact targets and exposes declared
framework choices. Future boards require compatible toolchain definitions;
universal hardware support is not guaranteed. Firmware reuse is allowed only
when source and target fingerprints match. Serial and editor failures use
bounded recovery; these recovery paths never replay uploads, erases, resets,
or commands.

See [Ubuntu and recovery details](direct/UBUNTU.md) and
[hardware-free verification](direct/verify_runtime.py). Windows regression and
Qt preview checks pass locally; native Ubuntu CI and hardware flashing remain
unverified in this workspace.

The project terminal passes xterm capability replies and bracketed paste to
interactive coding CLIs. Installed Codex, Claude Code and OpenCode version
commands have been verified through its Windows PTY; a local interactive probe
also checks Unicode paste, Ctrl+C, alternate screen and window dimensions.
Clear clears the display without typing into the active CLI. See
[terminal verification](direct/verify_terminal.py).

Devices with 4 or 6 physical cores (including CPUs with 8 or 12 SMT threads)
or at most 6 logical CPU threads use reduced editor animation,
background checking and terminal scrollback. Builds reserve two logical CPU
threads for the interface and OS (up to 2 compiler jobs on 4 threads, up to
4 on 6 threads, reduced further under RAM pressure). Saved job counts cannot
bypass this budget. Fewer than 4 physical cores blocks launch with an
incompatibility notice before GUI initialization or runtime repair. Unknown
logical CPU counts also stop startup with a diagnostic. When physical topology
is unavailable, the minimum is checked against logical threads.
Compiler concurrency also respects physical core count when the OS reports it.


| Feature | Description |
| --- | --- |
| **🖥️ Native PySide6 (Qt) Desktop UI** | High-performance, hardware-accelerated desktop interface built with PySide6 (`main/qt/`), featuring Glass Smoked Dark, Glass Frosted Light, and Solarized Dark themes with Montserrat typography and responsive splitters. |
| **🔨 Unified One-Click Build & Flash** | Exact PlatformIO board/framework builds with native programmer uploads for AVR, STM32, RP2040, SAM/SAMD, nRF52, Teensy and other declared targets; optimized ESP/AVR serial paths and incremental build caching. |
| **🛡️ Critical Operation Protection** | Safeguards against closing the application during sensitive hardware writes (flashing, flash erasing, bootloader recovery resets, and toolchain downloads) to prevent bricking microcontrollers or corrupting installations. |
| **📟 Advanced Serial Monitor & Auto-Reset Parity** | Real-time terminal with ANSI color rendering, timestamps, pause/resume, send bar, and high-throughput batch coalescing. Automatically issues a silent DTR/RTS pulse upon upload completion and focuses the monitor after a 500ms grace delay. |
| **⚡ Reset on Baud Change Toggle** | Configurable setting in Settings Dialog to automatically pulse DTR/RTS when switching baud rates (rebooting MCU into `setup()` at the new baud rate) or maintain uninterrupted execution. |
| **🔌 Zero-Reset Connection & Port Safety** | Passive port opening with explicitly de-asserted DTR/RTS control lines ensures connected ESP32 microcontrollers continue running active firmware without unintentional reboots. |
| **✏️ Offline Monaco Code Editor** | Embedded offline Monaco Editor (VS Code engine) via `QWebEngineView` and `QWebChannel` featuring C/C++ syntax highlighting, Go-To-Definition (`F12`), hover cards, and debounced auto-saving with zero CDN dependencies. |
| **🤖 Dedicated AI Assistant & Diff Glow** | Embedded OpenCode AI assistant with real-time file watcher, line-level LCS diffing, and animated pulsating diff glows (🟢 green added, 🔴 red removed) with a floating quick-dismiss banner. |
| **💻 Multi-Session Project Terminal** | Windows PowerShell/CMD ConPTY sessions and native Linux Bash PTYs rendered by offline xterm.js, with session tabs, coding CLI support, bounded output, and display-only Clear. |
| **🔄 Bootstrap-Prepared Offline Toolchains** | Bootstrap prepares declared frameworks, compiler variants, upload/debug tools, builder dependencies and configured sketch libraries. The workspace never downloads missing packages during compilation, upload or reset. |
| **🌐 Custom Offline Package Plan** | Add PlatformIO platform and library specifications to the bootstrap plan before offline use. The workspace's Bootstrap action starts separate host setup. |
| **📁 Remote Network Share (UNC) Support** | Seamless compilation and flashing of sketches stored on Windows SMB network shares (`\\server\share`) with automatic drive mapping and local SSD build acceleration. |
| **🔒 Strict Private Python Runtime Guard** | `private_python_guard.py` ensures the entire application runs strictly on the isolated bundled Windows runtime or native Linux virtual environment, eliminating conflicts or leaks with desktop/system Python. |
| **🚨 Session Sentinel & Crash Detection** | `crash_detector.py` provides automatic unhandled exception logging, session sentinel tracking, and startup crash recovery. |
| **🍃 Low-End Hardware Optimization** | Dynamic CPU core and RAM budgeting, `BELOW_NORMAL_PRIORITY_CLASS` subprocess scheduling, and generous silence watchdogs keep the UI responsive even on budget quad-core systems. |

### Startup and resource limits

All application dependency downloads belong to bootstrap. The default package
plan in [direct/offline-packages.json](direct/offline-packages.json) prepares AVR
and ESP32 with Arduino, including their required package variants and optional
upload/debug tools. Add platform specifications for megaAVR, SAM/SAMD, ESP8266,
STM32, RP2040, nRF52 or Teensy when needed. The optional `frameworks` list limits
preparation to those declared frameworks; omit it to prepare every framework
declared by the configured platforms. Bootstrap checks each distinct builder
environment before recording local readiness. Readiness certifies the configured
plan; installed boards outside that plan may need further preparation.
Incomplete preparation stops launch; a deleted package requires bootstrap repair.

On Windows, bootstrap omits the optional Unix-only Zephyr `tool-gperf` package
when none of the platform's board/framework configurations requires it. All
other declared package variants remain prepared, and required-package failures
still stop setup. Rerunning setup reuses packages that are already installed.

Windows setup checks the configured packages before fetching the pinned
`v1.0.0-assets` release seed. It verifies the archive's exact size and SHA256,
then imports only absent package groups with genuine PlatformIO metadata and
compatible versions. Already complete stores skip this download. Missing
packages are resolved normally afterwards; the release archive alone does not
certify readiness. Cached archives and failed transfer checkpoints are retained.
Publishing a verified release package retries only Windows access/sharing-lock
errors, for at most six seconds. Every attempt refuses an existing destination,
including a broken junction. Persistent denial retains the archive and extracted
staging with its error; setup never changes permissions or replaces another tree.
An incompatible existing destination stops with a repair diagnostic rather
than overwriting its files. Ubuntu prepares native packages separately.

Update detection and installation use the same target Python environment.
Versions are read afresh from that environment's distribution metadata, and
an upgrade is reported as verified only after the installed version is checked.
The application environment is kept separate from the base runtime; copying
individual package or metadata trees in either direction can cause recurring
update prompts. Missing dependencies use the target environment's pip instead.
Qt stays in that environment; setup does not download and install it again into
the base Python runtime. Windows pip uses a verified short interpreter spelling
and a short physical temporary folder to support wheel building and installation in
deeply nested folders without changing Windows' global long-path settings.
Incomplete checks report their diagnostics without an all-current summary.

Package planning preserves each configured registry owner and primary version.
When a framework changes a package's owner, an unqualified optional version
uses that observed owner only when the version has one clear owner across all
configurations. Explicit, ambiguous and unmatched variants remain declared;
builder checks use the versions actually selected for their framework.

Reviewed AVR/megaAVR Arduino builders share host preparation across MCUs with
the same exact core and package versions. Bootstrap validates their source
fingerprints first; unknown or modified builders keep separate MCU checks.
This reduces the reviewed AVR and megaAVR packs from 177 builder checks to 19.

Reviewed STM32 20.0.0 Arduino builders share preparation across boards using the
same exact core and full package specifications. The platform builder,
framework builder and nested variant script must match reviewed fingerprints.
This reduces those Arduino checks from 163 to 7; every other STM32 framework
keeps its distinct supported-target checks.

Zephyr 4.4.2 preparation uses an app-owned CMake alias file for the verified
STM32H747I Discovery M7, Nucleo H745ZI-Q M7 and Oceanus-I EV board renames.
Version and installed target metadata must match before applying aliases;
existing user aliases stay authoritative. Installed platform/framework files
are unchanged. STM32 20.0.0 advertises Zephyr for Ebyte E77, SparkFun MicroMod
STM32F405 and the Oceanus-I module, although that Zephyr package has no matching
board definitions. Setup reports these three unavailable combinations and
records the reasons in its prepared catalog. Their Arduino frameworks remain
available, and the board picker does not offer the missing Zephyr targets.
Unknown or changed definitions retain normal preparation and failure checks.
The same guarded availability check reports Mbed for Olimex STM32-H103 as
unavailable when Mbed 6.17 lacks its declared target. Its other frameworks
remain available. These exclusions require matching platform/framework
versions and an exact board-manifest fingerprint in the prepared catalog.

Builder preparation shows the current check and count, streams dependency
output, and reports elapsed time while a child is silent. Full output is kept
under `logs/offline-builders-*/builder-*.log`, including failures before the
bounded display tail. Environment dumps are omitted from these logs. Windows
bootstrap children receive Git long-path support without changing global Git
settings. A Zephyr module failure still stops setup and reports its full log.
Concurrent checks require reviewed builders that do not modify shared framework
state. Native and unknown builders run sequentially; separate temporary folders
alone do not isolate framework virtual environments or generated package files.
The reviewed AVR Arduino probes use the shared CPU/RAM/storage job budget.
`offline_bootstrap.py --jobs N` can reduce concurrency; requests above the safe
budget are capped. Each probe gets one compiler job to avoid nested job pools.
Windows Zephyr CMake uses the canonical framework base so junction-expanded
source paths produce consistent library names. Package and compiler arguments
keep their short paths. This applies during bootstrap and offline builds.

Windows ESP-IDF component objects use relative paths calculated from matching
canonical source and component directories. This keeps application and
bootloader objects in their separate build folders when the package store uses
a junction. Framework, tool and build arguments retain their short spelling;
installed framework scripts stay unchanged.

Mbed 6.17 preparation on both hosts uses validated `setuptools==80.9.0` and
`future==1.0.0` for its legacy Python APIs. The installed Mbed adapter selects
these prepared providers before its older bundled dependencies take precedence.
Setup validates their versions and imports; offline builds use them without
installing packages. Missing or incompatible providers require bootstrap repair.

The main app and its PlatformIO commands enforce offline operation. Missing
packages report the bootstrap repair command, and **Refresh boards** reads local
definitions. Editor, terminal and assistant frontend assets are bundled locally.
The toolbar's **Bootstrap** action opens a separate setup process. Add custom
platform and library specifications to the package plan before running setup;
the default library set includes Servo, ESP32Servo and ArduinoJson. An arbitrary
new sketch may need additional libraries in that plan. Bootstrap must prepare
them while online before the sketch can build offline.

Expanding the configured platforms and frameworks can take substantial time and disk space.
Subsequent launches verify local files without a network check. Windows and
Ubuntu need their own native prepared stores.

On a fresh Windows copy, Python's bundled Tk shows setup progress immediately
while Python dependencies install. After Qt passes verification, setup opens
the original Qt design before preparing board tools. The same worker, elapsed
timer, log, progress, preferences and reading position continue through this
change. Holding the scrollbar delays the switch until release. A display error
keeps the native window working; it does not restart installation. Worker output
uses one bounded queue through both views. Existing installations open the Qt
setup window directly when verification is needed.

The Windows bootstrap log shows Tool, Platform and Library Manager installation
confirmations in green with a check mark. Downloading and unpacking use separate
live progress rows, including updates emitted without a newline. Failed attempts
retain their last reported percentage with an interruption notice; mirror
warnings appear in yellow and final setup errors in red. Successful installation
confirms unpacking is complete even when PlatformIO omits its final percentage.
When a setup step fails, its entire retained output turns red, including its
subsections and subsequent progress. Earlier successful steps keep their colors.
Plain terminal output cannot recolor already printed scrollback.
Setup activity headings use subtle circuit dividers that resize with the reading
pane and stay out of copied log text. Major stages and nested checks retain a
clear hierarchy; update checking starts its own stage for enabled, skipped and
offline outcomes. The divider follows the stage's failure color.
Setup text retains its semantic hues with at least 4.5:1 contrast across the
static glass reading surfaces and solid native fallback, including Solarized
failures and selected log text. These derived inks leave the shared palette intact.

Build, serial and compatibility logs, syntax diagnostics, notification cards,
terminal ANSI colors, connection states and transient status messages use
readable semantic colors against their actual surfaces in the active theme.
Retained output is recolored when the theme changes. Terminals also enforce a
4.5:1 minimum contrast for CLI truecolor and indexed colors. Each workspace
propagates a theme change once to each panel, including a detached editor.
Build Console keeps the original journal: section dividers, the compiler worker
banner, each individual compiling filename, and the boxed timing breakdown.
All rows use the saved monospace content size so frames and columns stay aligned.
Warnings, errors, source excerpts and unfamiliar build messages remain visible;
routine PlatformIO promotional/status boilerplate stays out of the journal.
Copy includes all retained messages, even when warnings are hidden. Selection
copy copies the selected visible text. Saved timestamp preferences apply to the
journal. On narrow windows, Options holds the clear-on-action preferences so
Auto-scroll, Copy and Clear remain reachable.
Build delivery queues discard ordinary chatter before diagnostics under overload
and report omissions. The journal retains bounded history; Copy reports expired
history. Repeated valid progress updates are coalesced in small render batches,
and individual progress rows update without scanning the entire history.
Terminal control sequences are removed from displayed and copied build text,
including fragmented ANSI sequences. Unknown framework/custom-builder output,
compiler notes and SCons failures are preserved by the build-output router.
Bootstrap, build output, serial logs and
notifications preserve the visible text, selection and horizontal position
while output arrives. Bootstrap
Auto-scroll pauses while the scrollbar is held or dragged, and releasing it
resumes following the latest output even when released above the bottom.
Checking Auto-scroll also resumes following; checking it while holding the
scrollbar waits for release. Build output, serial logs and notifications keep
a scrolled-up reading position until the reader returns to the bottom.
Turning Auto-scroll off prevents forced following. If bounded history evicts
the visible text, the view clamps to the remaining history. The HTML assistant
fallback keeps its reading position; coding terminals retain their native
xterm behavior.
Notification filters use bounded retained history without rereading the database
for each selection. Revealing Notifications refreshes external activity in a
background worker; stale results from another project are discarded. Live
notifications follow the active filter, and failed Clear operations retain
history and show an error. Failed history writes show a transient warning;
notification replacement keeps the previous database intact on failure.
Status timers belong to the
window; older notifications and short actions cannot erase a newer action's
status. Instance settings and shared theme preferences are saved in one
transaction, with failed writes reported before applying changes. Successful
project switches release discarded editor snapshots while retaining current
dirty buffers and recovery state.

Windows bootstrap preserves the short package-store junction and uses extended
paths for archive extraction and verification, including builder subprocesses,
so deeply nested framework files can be prepared in long installation folders.
Package installation also uses contained extended paths when copying unpacked
archives and cleaning their staging directories. This preserves literal
trailing-dot TAR entries in older Windows ARM toolchains. Canonical containment
limits the adaptation to the configured store and staging directories; archive
security and package version checks stay active.
Bootstrap's bounded output queue retains warnings and errors during diagnostic
bursts, coalesces progress, and uses background-worker backpressure when every
queued event is critical. Closing setup releases a waiting worker.

Windows installations save a per-user health snapshot after successful setup.
Subsequent launches check local runtime paths and source fingerprints before
opening the GUI. First launch, a changed installation, missing dependencies,
explicit `--repair`/`--setup`, or a recorded crash runs verification again.
An immediate failed GUI launch falls back to repair. The project picker no
longer starts a hidden editor; services begin after the workspace is shown.
Port enumeration and catalog/USB discovery run in background workers.

Four/six-core and low-memory profiles use one syntax parser, eight-second
project-check intervals, 2,000 display history entries, 256,000 characters of
pending output per stream/layer and 512,000 characters of retained display
history. Larger systems use two parsers and larger bounded buffers. Worker logs
are bounded before entering Qt's event queue; render batches, very long lines,
notification text and source/diagnostic caches also have limits. Older display
output can be omitted under sustained overload, with a visible notice. These
display limits do not apply to coding-terminal protocol traffic, which uses
PTY/xterm backpressure. Clear also discards retained serial display history.

Editor parsing runs off the UI thread and keeps only the latest pending revision.
Syntax results return through queued Qt signals, so repeated checks recover
after a transient failure. Resize events share a settling timer, fonts register
once, and vector icons use a bounded cache.

A local Windows Qt benchmark with simulated project data measured the import
phase at **3.78 s before / 0.24 s after**, the longest console flush at
**56 ms / 3.8 ms**, and pending text for a 20,000-entry burst at
**20.1 MB / 0.25 MB**. These are component measurements on one host; complete
launcher time and actual four/six-core hardware performance vary. The editor's
base WebEngine memory footprint remains, while streaming/cached memory growth
is bounded. Use `direct/verify_performance.py` for isolated regression checks.

---

## 🚀 Quick Start

**Ubuntu:** follow [native setup and compatibility notes](direct/UBUNTU.md). Run `python3 direct/ubuntu/setup.py`, then `bash direct/ubuntu/run.sh`. Repair with `bash direct/ubuntu/run.sh --repair`. Setup and native dependencies live in `direct/ubuntu/`.

Windows files live in `direct/windows/`. Host toolchain implementations are separate in `main/platforms/windows.py` and `main/platforms/ubuntu.py`; they share the editor, project windows and CPU/RAM budgets. Ubuntu uses its native virtual environment and architecture-specific PlatformIO store. The older launch paths remain compatibility forwarders.

**Windows:**

### Launching the Application

```cmd
# Double-click the compiled native Windows launcher:
MCU_Flasher.exe

# Or launch via the bootstrap launcher (runs under standard user privileges):
direct\windows\run.vbs

# Or run directly using the private Python environment in a terminal:
python mcu_flash_gui.py
```

### First-Run Auto-Bootstrap Pipeline

Setup uses smoked glass cards, static reflections and an original circuit-chip icon family shared with the workspace. The first-run Tk view and the later Qt view both open with a clean activity summary. Larger, bold stage headers separate Python, board tools, drivers and update checks, with indented results beneath each header. Package progress occupies compact rows; completed groups collapse to a count. **Technical details** reveals the retained installer output, while the detailed run log keeps the full diagnostics. Resolver chatter and repeated success messages stay out of the default summary; warnings and failures remain visible. The status shows a short action or package count instead of a long dependency list.

The display handoff preserves both logs, selections, reading positions, the details disclosure, progress and preferences without restarting setup. Auto-Scroll applies to both views: a held scrollbar pauses following, release resumes when enabled, and Auto OFF preserves the reading position. Glass painting is cached and changes only with size, palette or actual setup state.

Setup starts at 70% of the active monitor's width and height. Qt logical sizing, preferred 520×420 floors and native frame fitting to the usable work area keep it usable on smaller displays; it remains resizable. Tk keeps its own native metrics.

1. **Drive Storage Verification**: Inspects installation drive for storage speed and integrity.
2. **Private Python Auto-Healing**: Verifies and heals the isolated portable Python 3 runtime at `src/_python/` using bundled offline installers in `installers/.handsoff/`.
3. **Virtual Environment Isolation**: Configures and validates required dependencies (`PySide6`, `pyserial`, `pywebview`, `pywinpty`).
4. **Pre-Built Toolchain Seeding**: Seeds the pre-built PlatformIO core (~1.7GB fast download with resume & SHA-256 verification) and Arduino CLI binaries.
5. **Driver Verification**: Detects Silicon Labs CP210x and CH34x USB UART drivers; Windows displays a UAC prompt only if a missing machine-level driver installation is strictly required.
6. **Offline Package Preparation**: Installs every configured platform's declared framework and uploader/debugger package variants, prepares builders and sketch libraries, and verifies local editor/terminal assets. Bootstrap writes readiness only after the whole plan succeeds.
7. **GUI Launch**: Boots the native PySide6 desktop interface with smooth layout transition. Direct GUI entry points require bootstrap readiness and never run installers.

> [!IMPORTANT]
> **Python Requirement (Git Source vs. Release Package)**:
> - **Pulling from Git (Source)**: Pulling and running directly from this repository requires **Python (3.10+) installed on your machine** so the initial launch scripts (`launcher.py`, `runThisOnWindows.vbs`) can run and auto-bootstrap the private runtime.
> - **Release Package (`MCU_Flasher.exe`)**: Pre-built standalone release packages are completely self-contained — **no Python installation is required** on the machine.

> [!NOTE]
> **Storage Requirement**: The configured offline plan can require substantial space and download time, especially with more platforms or native frameworks. Its size depends on the current packages; the older 6 GB baseline covers only a limited toolchain set. Add custom platforms, frameworks and libraries to `direct/offline-packages.json` before running bootstrap while online.

> [!IMPORTANT]
> **Hardware Requirement**: At least **4 CPU cores** are required. Startup checks physical cores when available and uses logical threads as a fallback when the OS cannot report physical topology. Unsupported systems receive an incompatibility notice before the application starts.

> [!TIP]
> **Manual Hardware Gating**: MCU Flasher launches cleanly with no board and no COM port pre-selected. An MCU already connected when the app opens is never reset on connection; reset happens only through the physical/on-app reset control or after an upload.

---

## 📁 Project Structure

Below is the complete architectural layout of the MCU Flasher ecosystem:

```
MCU Flasher by Naph/
├── MCU_Flasher.exe                   # Native Windows launcher (compiled from src/launcher.cs)
├── mcu_flash_gui.py                 # Root application entry point forwarder
├── README.md                         # Comprehensive documentation, user guide & architecture
├── LICENSE                           # MIT license terms
│
├── main/                             # Native PySide6 (Qt) Desktop UI & Backend Engine
│   ├── __init__.py                  # Public exports (MCUWebBackendAPI, main)
│   ├── mcu_flash_gui.py             # PySide6 application lifecycle, core checks & window bootstrapper
│   ├── web_bridge.py                # Centralized thread-safe backend engine & toolchain controller
│   │
│   ├── platforms/                   # Separate host toolchain/process implementations
│   │   ├── windows.py              # Windows bootstrap, executable discovery and short paths
│   │   └── ubuntu.py               # Native Python/PlatformIO, POSIX sessions and Linux paths
│   │
│   ├── qt/                          # Native PySide6 (Qt for Python) Desktop UI Panels
│   │   ├── __init__.py              # Qt package initializer
│   │   ├── main_window.py           # MCUMainWindow root window with resizable splitters & dock tabs
│   │   ├── toolbar.py               # Action controls, board/port dropdowns, baud selector & telemetry
│   │   ├── editor_panel.py          # Embedded Monaco Code Editor host (QWebEngineView + QWebChannel)
│   │   ├── console_panel.py         # Colorized build and upload console output with ANSI regex parsing
│   │   ├── serial_panel.py          # Real-time serial monitor with baud control, send bar & timestamp
│   │   ├── terminal_panel.py        # Embedded multi-session terminal panel (PowerShell & CMD tabs)
│   │   ├── posix_terminal_panel.py  # Native Linux PTY terminal and optional OpenCode assistant
│   │   ├── glass.py                 # Static workspace surface and focused tool-tab navigation
│   │   ├── icons.py                 # Theme-aware vector action icons
│   │   ├── ai_panel.py              # Collapsible OpenCode AI assistant panel with session management
│   │   ├── compat_panel.py          # Compatible boards and MCU pinout compatibility viewer
│   │   ├── syntax_panel.py          # Real-time C++ syntax checker diagnostic tree
│   │   ├── notif_panel.py           # Per-project notification and event log viewer
│   │   ├── detached_editor.py       # Independent popped-out Monaco Editor window wrapper
│   │   ├── settings_dialog.py       # Preferences modal (themes, CPU jobs, auto-save, baud reset)
│   │   ├── project_dialog.py        # Project selector & new project scaffolding wizard
│   │   ├── modify_dialog.py         # Project sketch file management dialog (add, rename, delete)
│   │   ├── download_dialog.py       # Explicit handoff to a separate bootstrap process
│   │   ├── theme.py                 # Multi-theme QSS stylesheet generator (Glass Dark, Glass Light, Solarized)
│   │   └── signals.py               # Centralized QtSignalBus for thread-safe cross-thread event routing
│   │
│   └── core/                        # Core Foundations & System Services
│       ├── __init__.py              # Lazy core exports without host import cycles
│       ├── constants.py             # Global constants, regex patterns, baud rates, headers & telemetry
│       ├── theme.py                 # Theme color tokens, font definitions, and dark/light styling rules
│       ├── config.py                # Config persistence (gui_config.json) & multi-instance PID locks
│       ├── file_utils.py            # Windows attributes (attrib +h), UNC path detection, robust file I/O
│       ├── toolchain.py             # Stable API selecting only the current host implementation
│       ├── build_resources.py       # Shared CPU, RAM and storage worker budgets
│       ├── board_catalog.py         # Installed/cached/registry board definitions and framework choices
│       ├── target_profile.py        # Exact target validation and upload transport requirements
│       └── board_compat.py          # Board compatibility detection & GPIO pin conflict analyzer
│
├── src/                              # Core System Modules, Offline Assets & Runtime Guards
│   ├── _python/                     # Private portable Python 3 runtime (isolated, attrib +h)
│   ├── .platformio-mcu-gui/         # PlatformIO core store (junctioned to avoid MAX_PATH issues)
│   ├── gui_config.json              # Persisted user settings (themes, baud rates, auto-save, CPU mode)
│   ├── syntax_checker.py            # Standalone C++ syntax linter & AST analyzer
│   ├── launcher.cs                  # Native Windows C# launcher source
│   ├── launcher.cpp                 # Native Windows C++ executable wrapper source
│   ├── resources.rc                 # Windows executable resource definition (icon embedding)
│   │
│   ├── modules/                     # System Services, Bootstrap Pipeline & Execution Drivers
│   │   ├── bootstrap.py             # Windows runtime bootstrapper & unattended dependency installer
│   │   ├── launcher.py              # Application coordinator & Windows AppUserModelID configurator
│   │   ├── private_python_guard.py  # Strict private Python runtime enforcer (halts system Python leaks)
│   │   ├── crash_detector.py        # Session sentinel, crash event recorder & unhandled exception hook
│   │   ├── dedicated_AI.py          # OpenCode AI assistant process controller & file watcher
│   │   ├── project_terminal.py      # Standalone project terminal server & PTY backend
│   │   ├── platform_runtime.py      # Host detection, native paths, locks and CPU startup guard
│   │   ├── runtime_resources.py     # Shared CPU/RAM budgets for builds, editors and terminals
│   │   ├── recovery.py              # Bounded transient recovery without command replay
│   │   ├── arduino_lib_req.py       # Glass Arduino library, board-package and installed-item browser
│   │   ├── tk_glass.py              # Static glass cards and contrast helpers for the downloader
│   │   ├── ui_palette.py            # Toolkit-independent color mixing and button contrast
│   │   ├── detector.py              # USB serial port auto-detection & board probing
│   │   ├── downloader.py            # Resumable downloader with SHA-256 verification & GDrive support
│   │   ├── win_subprocess_hide.py   # Windows CREATE_NO_WINDOW background subprocess suppressor
│   │   ├── reset_editor.py          # Historical settings tool; not Qt renderer recovery
│   │   ├── setup_ide_paths.py       # Dynamic compile_commands.json path re-navigator
│   │   └── get-platformio.py        # Bundled official PlatformIO installer script
│   │
│   ├── editor/                      # Offline Monaco Editor Web Engine (Zero CDN Dependency)
│   │   ├── index.html               # Monaco iframe host with QWebChannel bridge & diff animation CSS
│   │   ├── terminal.html            # Offline Linux xterm.js frontend with PTY acknowledgements
│   │   ├── bundle.js                # Self-contained offline Monaco code editor engine
│   │   ├── qwebchannel.js           # Bidirectional Qt-to-JavaScript communication bridge
│   │   ├── 18.bundle.js             # Monaco Editor async chunk (lazy-loaded features)
│   │   ├── editor.worker.js         # Monaco Editor web worker (tokenization & validation)
│   │   └── *.ttf                    # Bundled offline editor icons and font assets
│   │
│   ├── assets/                      # Shared Graphics & UI Assets
│   │   ├── mcu_icon.ico             # Application icon
│   │   ├── icons/                   # Custom SVG/PNG checkboxes and control assets
│   │   └── xterm/                   # xterm.js terminal web distribution
│   │
│   └── dbs/                         # Persistent JSON Databases & Notification Store
│       ├── bootstrap_config.json    # Bootstrap update, check, and skip configuration
│       ├── arduino_browser_settings.json  # Board browser user preferences
│       ├── arduino_cli_path.txt     # Cached Arduino CLI binary path
│       ├── dbs_notif.json           # Global notification fallback store
│       ├── dbs_create.py            # Notification DB CRUD: create
│       ├── dbs_read.py              # Notification DB CRUD: read
│       ├── dbs_update.py            # Notification DB CRUD: update
│       └── dbs_delete.py            # Notification DB CRUD: delete
│
├── direct/
│   ├── windows/run.vbs             # Silent Windows launcher with CPU guard
│   ├── ubuntu/run.sh               # Native Ubuntu virtual-environment launcher
│   ├── ubuntu/setup.py             # Repairable Ubuntu setup without system pip writes
│   ├── ubuntu/requirements.txt     # Native Linux dependencies
│   ├── runThisOnWindows.vbs         # Compatibility forwarder to windows/run.vbs
│   ├── runThisOnUbuntu.sh           # Compatibility forwarder to ubuntu/run.sh
│   ├── setup_ubuntu.py              # Compatibility forwarder to ubuntu/setup.py
│   ├── UBUNTU.md                    # Linux setup, permissions and verification limits
│   ├── verify_platforms.py          # Isolated host selection, launch syntax and native paths
│   ├── verify_runtime.py            # Hardware-free runtime and real Qt/Monaco checks
│   └── verify_terminal.py           # Windows ConPTY/xterm interactive protocol checks
│
├── installers/                      # Offline Binaries, Toolchains & USB Drivers (Git LFS)
│   ├── .handsoff/                   # Portable Python runtime installers (python-*-amd64.exe)
│   ├── CP210x/                      # Silicon Labs CP210x USB-to-UART driver installer
│   ├── CH34x/                       # WCH CH340 / CH341 USB serial driver installer
│   ├── arduino-cli.msi              # Bundled Arduino CLI installer
│   ├── MicrosoftEdgeWebview2Setup.exe # Bundled Microsoft Edge WebView2 installer
│   └── msys2-*.exe                  # Bundled MSYS2 build tools
│
├── soft_reset/                      # App-Owned Reset Templates & Exact-Board Caches
│   ├── soft_reset_project/          # Minimal PlatformIO reset template for ESP32 boards
│   ├── soft_reset_project_uno/      # Minimal PlatformIO reset template for Arduino AVR boards
│   ├── soft_reset_project_esp8266/  # Minimal PlatformIO reset template for ESP8266 boards
│   ├── soft_reset_project_stm32/    # Minimal PlatformIO reset template for ST STM32 boards
│   ├── soft_reset_project_rp2040/   # Minimal PlatformIO reset template for Raspberry Pi Pico / RP2040
│   ├── soft_reset_project_samd/     # Minimal PlatformIO reset template for SAM / SAMD (Zero) boards
│   ├── soft_reset_project_teensy/   # Minimal PlatformIO reset template for PJRC Teensy boards
│   ├── soft_reset_project_nrf52/    # Minimal PlatformIO reset template for Nordic nRF52 boards
│   └── soft_reset_project_renesas/  # Minimal PlatformIO reset template for Renesas RA (UNO R4) boards
│
├── cleaner/                         # Maintenance Utilities
│   ├── clean_fresh.bat              # Workspace refresh cleaner
│   ├── clean_pycache.bat            # Recursive Python cache cleaner
│   └── terminate_all_python.exe-task.bat # Kill background Python processes
│
├── DANGER-ZONE/                     # Destructive Reset Utilities (Use with Caution)
│   ├── DELETE_EVERYTHING_DO_NOT_RUN.ps1 # Full environment teardown script
│   └── runReset.cmd                 # Reset launcher
│
├── index_json/                      # Board and library index caches
├── .mcu_flasher_build_cache/        # Per-board build caches and workspaces (hidden, gitignored)
└── logs/                            # Runtime diagnostic logs, crash dumps & mutex locks
```

---

## 📘 User Guide & How to Use

### 1. Launching & First-Run Auto-Bootstrap
- Launch the application by double-clicking **`MCU_Flasher.exe`** (or running **`direct\runThisOnWindows.vbs`**).
- **Private Python Runtime Enforcer**: Handled transparently by `src/modules/private_python_guard.py`. Windows uses its bundled private runtime. Linux uses `.venv-linux` (or a valid local Linux `env` virtual environment) and provides a setup command when it is missing.
- **Session Sentinel & Crash Detection**: `src/modules/crash_detector.py` monitors runtime integrity, providing unhandled exception logging and clean startup recovery markers.
- Bootstrap verifies runtime dependencies and prepares the configured board/framework/tool/library packs before opening the app. Compile, Upload and Reset use installed packages only.
- Normal startup and serial monitoring operate with current user permissions; Windows prompts for UAC elevation only when a missing driver or system component strictly requires it.

### 2. Opening, Selecting & Scaffolding Projects
- Click **Project** or press **Ctrl+O** to choose whether a selected or newly created sketch opens in the current window or a new one. Switching the current window prompts to save, discard or cancel when editor changes are unsaved. Choosing a new window keeps the current editor, terminal, build output and board/port state intact; new windows start with hardware unselected. The startup picker opens the first project directly.
- **Open projects** lists running sketch windows and their folder paths. Use **Show window** to return to a project. Reopening a sketch brings its existing window forward; one sketch and one serial port can belong to only one window at a time. Use separate ports to monitor or upload to different boards concurrently.
- Right-click the project title or click its folder icon to reopen the picker. **Cancel** leaves the current editor and monitor sessions intact. Choose a new window to open another project while the current one compiles; choosing the current window is blocked while an action is in progress.
- **New project** scaffolds a sketch with optional header/source files and asks where to open it. An existing folder is never overwritten; choose it through **Existing project** instead.
- **Modify Project Files**: Click **`📝 Modify Files`** to create new files, rename existing files, or delete sketch files (`.ino`, `.cpp`, `.h`).

### 3. Selecting Boards & COM Ports
- **Manual Hardware Selection**: MCU Flasher opens with no board and no port pre-selected (`""`). You retain full control over target hardware, preventing unintentional flashing or port locking.
- **Active COM Port Enumeration**: The port dropdown enumerates all active serial ports with hardware descriptions (e.g. `COM9 - USB-SERIAL CH340 (COM9)`). Selecting a port connects instantly.
- **Zero-Reset ESP32 Protection**: Serial ports open with DTR and RTS explicitly de-asserted (`conn.dtr = False`, `conn.rts = False`). Background `esptool` probing is disabled, ensuring running firmware on an attached ESP32 continues running smoothly without an unintended reset.
- **Board Catalog Search**: Click **`🔍 Search Boards`** to search the available catalog by board name, chip, architecture, vendor or board ID. Search uses a background worker, a short typing debounce and a virtual list; repeated queries and reopening reuse bounded caches. Architecture filters, recent boards, framework selection and typo fallback remain available. Opening uses the current catalog; **Refresh boards** explicitly checks for newer definitions.
- **Additional board platforms and libraries**: Add their PlatformIO specifications to `direct/offline-packages.json`, then open **Bootstrap** or run the host setup command while online. The workspace never opens the network downloader.

### 4. Compiling & Flashing Code
- **Compile Only (`🔨 Compile`)**:
  - Available as soon as a board is selected, even without a serial port. The build pipeline checks the exact board definition and framework and reports missing definitions when invoked.
  - An older cached Arduino row with no PlatformIO board ID is repaired against this app's installed manifests before compilation. Matching recognizes the manifest's declared vendor prefix and Arduino build-define prefix, preserves the selected board name, and rejects ambiguous targets. Missing definitions require bootstrap preparation; runtime never queries the online registry.
  - Compiles the sketch using the PlatformIO SCons engine or Arduino CLI.
  - *Non-Blocking Execution*: The Serial Monitor remains active, streaming, and fully interactive during compilation!
  - Caches intermediate objects in `.mcu_flasher_build_cache/boards/<board-key>/` for near-instant incremental rebuilds.
  - **Offline Toolchain Preparation**: Add a board platform or library to `direct/offline-packages.json`, then run bootstrap while online. The main app blocks registry/VCS package installation and uses the prepared local store.
  - **Resource-Aware Throttling**: Checks physical RAM and logical CPU cores via `psutil`. Background subprocesses are scheduled with `BELOW_NORMAL_PRIORITY_CLASS` (`0x00004000`), ensuring the UI, Monaco editor, and serial monitor stay fully responsive even during heavy compiles.
- **Upload Firmware (`⚡ Upload`)**:
  - Serial uploads require a selected board and port. Boards with verified native USB/programmer interfaces (such as ST-Link, J-Link, CMSIS-DAP, picotool, DFU or Teensy) can upload without a serial port; the tooltip identifies this interface. Unknown transports require a port. Buttons and keyboard shortcuts recheck selection and busy state after saving.
  - Compiles modified files and flashes the binary to the microcontroller.
  - Features unit-aware byte parsing for modern `esptool` v5.4.0+ outputs.
  - Automatically pauses the Serial Monitor during the write phase to release port contention, then auto-resumes monitoring once flashing finishes.
  - Uses the exact board's declared protocol and bootloader speed. Nano ATmega328 keeps its 57600-baud default; Uno and Mega keep theirs. The speed control shows the board default or **Auto** on non-ESP boards. Native programmers never receive a serial-port argument, and hardware writes are never automatically replayed.
- **Framework compatibility**: Select a framework declared by the board in the picker. `.ino` files require Arduino; native C/C++ projects can use other declared frameworks. First-use preparation resolves their own packages rather than compiling an Arduino placeholder. Framework and board-definition changes use distinct firmware/cache identities. BIN, HEX, UF2 and ELF artifacts are recognized. Exact PlatformIO definitions, compatible source/libraries, packages and hardware drivers remain required; arbitrary unsupported boards are not inferred from their family name.
- **Stop Operation (`🛑 Stop`)**: Cancels an active compilation, upload, or resets a hanging serial session.

### 5. Live Serial Monitor & Post-Upload Auto-Reset
- View real-time MCU serial output in the bottom **Serial Monitor** tab.
- **Upload & Reset Parity**:
  - Upon a successful upload, hard reset, or soft reset, the application automatically switches the bottom view to the **Serial Monitor** after a 500ms grace delay.
  - After upload completion, MCU Flasher issues an automatic silent DTR/RTS reset pulse, guaranteeing the MCU reboots into user application mode and starts streaming boot logs immediately.
  - **Error Visibility Retention**: If an operation fails (`success=False`), the focus strictly remains on the **Build Console** so error traces stay visible and actionable.
- **Reset on Baud Change Toggle**:
  - Configurable in the Settings Dialog (`reset_on_baud_change`).
  - When enabled, switching baud rates triggers a hardware DTR/RTS pulse so microcontroller `setup()` re-runs at the new speed.
  - When disabled (default), baud rates switch cleanly without resetting the microcontroller.
- **High-Throughput Optimization**:
  - Batch chunk coalescing, queue backlog clamping, and smart timestamp bypass eliminate GUI lag during high baud rate streaming (up to 921,600 / 2,000,000 baud).

### 6. Critical Operation Protection & Safe Shutdown
- The application actively protects against closing the window or interrupting sensitive operations:
  - **Toolchain / Framework Downloads**: Prevents closing while downloading core packages to avoid corrupting the PlatformIO installation.
  - **Firmware Uploads & Writing**: Prevents closing while firmware is actively being written to flash memory, preventing unbootable or corrupted MCU states.
  - **Flash Erasing**: Blocks closing during full chip erase operations to prevent half-erased chip lockouts.
  - **Hardware / Soft Resets**: Protects bootloader and recovery write sequences.
  - **Cache Cleaning**: Ensures directory purges finish cleanly.
- If a close attempt is made during these critical operations, a descriptive warning dialog explains what is running and safely ignores the close event.
- Pure compilation (`op == "compile"`) is safely cancellable upon closing.

### 7. Offline Monaco Code Editor
- **Monaco Editor (VS Code Engine)**:
  - Embedded offline via `QWebEngineView` and `QWebChannel` (`src/editor/qwebchannel.js`).
  - 100% offline: zero CDN dependencies, local bundled scripts, web workers, and font assets.
  - Features: Multi-tab editing with drag reordering, C++ autocomplete, F12 / Ctrl+Click Go-To-Definition, Ctrl+Hover documentation cards, and real-time syntax checking.
  - Source tabs support keyboard navigation and announce selection and unsaved changes. Workspace tool tabs shorten labels in compact windows and keep full tooltips.
  - **Detach Editor** moves the existing editor into a separate window. Closing that window reattaches it; files and dirty buffers stay in memory.
  - Debounced auto-saving (customizable delay in Settings).
  - Keyboard shortcuts: `Ctrl+R` to Compile, `Ctrl+U` to Upload, `Ctrl+S` to Save All, `Ctrl+` / `Ctrl-` to zoom.

### 8. Multi-Session Project Terminal
- Click **Terminal** in the bottom dock.
- **Session management**:
  - **`[+]` New Terminal**: A click opens **PowerShell (`pwsh`)**. Use the adjacent arrow to choose PowerShell or **Command Prompt (`cmd`)** explicitly. With no session tabs, the button sits at the header's left edge; it follows the tabs when sessions are open.
  - **Dynamic Session Tab Bar**: Tab chips for every active terminal session with active state highlighting and individual close buttons (**`✕`**).
  - **Clear**: Clears the active terminal display and retained display history without sending a command to a shell or coding CLI.
  - **`[🗑 Kill]`**: Destroys the active terminal session, terminating the background PTY worker.
  - Resize the terminal with the workspace splitter; the terminal header has no Full/Restore toggle.
- **Zero-Session Idle State**: Starts cleanly with zero open sessions and seamlessly transitions to an idle placeholder when all sessions are closed.
- **Windows**: PowerShell/CMD use real ConPTY sessions (`pywinpty`) in an isolated child process with WebView2/xterm.js rendering. Each session receives its own window dimensions.
- **Ubuntu**: Bash uses native PTYs with Qt WebEngine and offline xterm.js. The terminal preserves PATH so installed coding CLIs can run.
- Capability replies, alternate screens, Ctrl+C, Unicode and bracketed paste pass through the PTY. Output acknowledgements limit queued data when the renderer is slow. Shell failures are visible; sessions and commands are never automatically replayed.

### 9. OpenCode AI Assistant & Pulsating Diff Glow
- Click the **`🤖 AI Assistant`** button on the toolbar to open the embedded AI side panel.
- Chat with OpenCode, generate code, or request refactoring.
- **Live AI Diff & Pulsating Glow**: When the AI modifies files in your sketch, Monaco Editor automatically detects the change, reloads the file, and highlights modifications with pulsating glowing animations:
  - 🟢 **Green Glow** on added / modified lines.
  - 🔴 **Red Glow** on removed lines.
  - Floating banner with line count summaries and a quick **"Dismiss Glow ✖"** button.

### 10. Soft Reset & Hard Reset Recovery Flashing
- **Soft Reset**: Flashes a minimal Arduino routine through a resolved board definition that declares Arduino support. Other frameworks and unresolved targets receive an explanation. This writes firmware and is not a generic reset for arbitrary hardware.
- **Hard Reset**: ESP32 validates/builds exact-board recovery images before erasing, then restores the bootloader, partitions and boot_app0 without application firmware. ESP8266 erases SPI flash and retains its ROM bootloader. Other targets, including AVR bootloader burning without a configured programmer workflow, are unavailable in Settings.
- Reset confirmations recheck board, framework, port and busy state. Workers reserve the operation before starting, retain the confirmed target, and report preparation/erase/write failures. Reset writes are not automatically replayed after a connected write failure.
- **Actions**: Save All acknowledges completed editor saves before Compile/Upload. Failed saves stop the action. Reload uses Monaco's reload and dirty-tab bookkeeping. Clean removes known generated build/reset caches while preserving source folders, project settings and AI edit history; locked or failed cleanup is reported.
- Hardware-free reset/action regressions: `src/_python/python.exe -B direct/verify_actions.py` and `direct/verify_controls.py`. Physical reset/flash verification requires the selected board and port.

### 11. Remote Network Shares (UNC Paths)
- Open and compile projects directly from Windows network storage (e.g. `\\server\share\sketch`).
- MCU Flasher dynamically maps a temporary drive letter during build execution and routes intermediate object files to local fast SSD storage (`remote_workspaces/`), eliminating SMB `.sconsign*.dblite` locking errors.

---

## 🧭 Architectural Reference: What is What & Which is Which

### Root Entry Points & Launchers

- **`MCU_Flasher.exe`**: Native C# wrapper compiled from `src/launcher.cs`. Starts `direct\runThisOnWindows.vbs` silently without prompting for unnecessary Administrator elevation.
- **`direct/windows/run.vbs`**: Windows VBScript bootstrapper that checks drive storage type and launches `src/modules/launcher.py`. Requests targeted UAC elevation only for machine-level driver installations. `direct/runThisOnWindows.vbs` forwards here for existing executable launchers.
- **`direct/ubuntu/setup.py` / `run.sh`**: Native Ubuntu setup and launch. Create/repair `.venv-linux`, preserve project arguments and keep system Python unchanged; `run.sh --repair` explicitly repairs the runtime.
- **`mcu_flash_gui.py`**: Root entry point forwarder. Enforces private Python execution via `private_python_guard.py`, installs console-hiding hooks, and delegates directly to `main.mcu_flash_gui.main()`.

---

### The Native PySide6 (Qt) Desktop Architecture & Web Bridge Engine

- **`main/mcu_flash_gui.py`**: Standalone native desktop launcher. Configures high-DPI scaling, Windows taskbar grouping (`naph.mcuflasher.gui.v3`), minimum 4 CPU cores verification, single-instance mutex locking, session tracking via `crash_detector.py`, and initializes the native PySide6 `QApplication`.
- **`main/web_bridge.py` (`MCUWebBackendAPI`)**: Centralized, thread-safe asynchronous backend engine. Coordinates compiler pipelines, COM port serial streaming, telemetry, board catalog search, and project operations, routing all state changes to the UI via `QtSignalBus`.
- **`main/qt/`**: Modular PySide6 interface suite:
  - **`main_window.py`**: Root window with action toolbar, resizable `QSplitter` containers, dockable bottom panels (Serial Monitor, Build Console, Terminal, Compatible Devices, Syntax Diagnostics, Notifications), and status bar.
  - **`toolbar.py`**: Primary control bar (Compile, Upload, Clean, Reset, Project, Settings), target board dropdown, COM port selector, baud rate selector, and status indicators.
  - **`editor_panel.py`**: Embedded offline Monaco Code Editor hosted via `QWebEngineView` with real-time bidirectional communication via `QWebChannel`.
  - **`console_panel.py`**: Colorized build output with regex ANSI color parsing and autoscroll.
  - **`serial_panel.py`**: High-performance real-time serial monitor with line ending selector, baud rate dropdown, timestamp toggling, and quick send bar.
  - **`terminal_panel.py`**: Multi-session integrated terminal panel supporting PowerShell and CMD tabs.
  - **`posix_terminal_panel.py`**: Native Linux Bash PTYs and optional OpenCode integration using Qt WebEngine/xterm.js.
  - **`glass.py` / `icons.py`**: Static glass workspace, focused tool tabs and theme-aware vector icons.
  - **`detached_editor.py`**: Explicit editor window and themed attach placeholder; closing reattaches the same editor.
  - **`ai_panel.py`**: Collapsible OpenCode AI assistant side panel.
  - **`syntax_panel.py`**: Interactive AST syntax diagnostic tree with line jump navigation.
  - **`compat_panel.py`**: Board compatibility matrix and GPIO pinout inspector.
  - **`notif_panel.py`**: Per-sketch notification log viewer.
  - **`settings_dialog.py` / `project_dialog.py` / `download_dialog.py` / `modify_dialog.py`**: Native Qt modal dialogs.
  - **`theme.py`**: Precision dark/light QSS stylesheet engine supporting Glass Smoked Dark, Glass Frosted Light, and Solarized Dark themes.
  - **`signals.py`**: Centralized `QtSignalBus` maintaining thread-safe Qt signals for all worker-to-UI communication.

---

### The `main/core/` Foundation Modules

- **`main/core/constants.py`**: Immutable constants, regex patterns (`ANSI_CSI_RE`, `_ESPTOOL_*_RE`), default baud rates, C++ standard headers, and telemetry helpers.
- **`main/core/theme.py`**: `Theme` class holding color tokens, typography parameters, and visual styles.
- **`main/core/config.py` / `config_store.py`**: Manage portable/per-user preferences, atomic project/serial-port ownership and recent projects. Locked snapshot merging preserves registrations and unrelated preferences when multiple sketch windows save at once.
- **`main/core/file_utils.py`**: Low-level Windows file operations (`attrib +h`, `ensure_file_writable`, `robust_rmtree`), UNC share detection (`is_unc_or_network_path`), and AI review history backups (`AIEditBackupStore`).
- **`main/core/toolchain.py`**: Stable API selecting `main/platforms/windows.py` or `main/platforms/ubuntu.py`. Windows owns executable discovery and short-path junctions; Ubuntu owns native package paths and POSIX process sessions. Both implementations use guarded, installed-only PlatformIO commands. Ubuntu replaces inherited Windows PlatformIO paths and uses native upload for every board family.
- **`main/core/build_resources.py`**: Shared compiler/background CPU, RAM and storage budgets.
- **`main/core/board_catalog.py`**: Dynamic board catalog parser (merging PlatformIO and Arduino index boards), USB VID/PID mapping table, and unit-aware `_parse_byte_size` for modern `esptool`.
- **`main/core/board_compat.py`**: Heuristic board compatibility analyzer and pinout GPIO conflict checker.

---

### The `src/modules/` System Services & Runtime Guards

- **`src/modules/private_python_guard.py`**: Strict private Python runtime enforcer. Halts execution if triggered by system/desktop Python and seamlessly re-launches under `src/_python/python.exe`.
- **`src/modules/crash_detector.py`**: Session sentinel, unhandled exception recorder, and startup crash recovery marker engine.
- **`src/modules/bootstrap.py`**: Windows runtime bootstrapper with a resizable native PySide6 glass setup window, bounded logs and inline package progress. Repairs Python and dependencies, prepares PlatformIO, and launches the workspace. A healthy Windows installation uses the cached launch path; Ubuntu uses its native virtual environment.
- **`src/modules/offline_bootstrap.py`**: Prepares `direct/offline-packages.json` before workspace launch and certifies host-native dependencies. An explicit custom plan can be supplied with `direct/windows/run.vbs --repair --plan "path/to/plan.json"` or `python3 direct/ubuntu/setup.py --plan "path/to/plan.json"`. Changing the default plan requires bootstrap preparation again.
- **`src/modules/offline_runtime.py` / `offline_platformio.py`**: Prevent workspace package downloads and dependency installer fallbacks. Missing local packages report the host bootstrap repair command; local terminal sockets and symlinked libraries remain usable.
- **`src/modules/launcher.py`**: Entry point launcher configuring Windows `AppUserModelID` for taskbar grouping and single-instance mutex handling.
- **`src/modules/dedicated_AI.py`**: OpenCode AI assistant process controller (HTTP server + WebSocket + pywinpty).
- **`src/modules/project_terminal.py`**: Standalone project terminal server & PTY backend for integrated multi-terminal sessions.
- **`src/modules/win_subprocess_hide.py`**: Enforces `CREATE_NO_WINDOW` on all background subprocesses.
- **`src/modules/arduino_lib_req.py`**: Tk/ttk glass browser for Arduino libraries, board packages and installed items. Keeps search, version selection, checksum verification and cancellation; **Board indexes** reveals advanced vendor URLs. **Quit** releases the process; closing the window keeps its catalog ready to reopen.
- **`src/modules/tk_glass.py`**: Static bordered glass cards with resize-coalesced highlights and readable button colors. No compositor blur or animation loop.
- **`src/modules/browser_loading.py`**: Source-validated compact catalog caches, atomic background cache writes, bounded disk scans and demand-driven Tk callback delivery.
- **`main/qt/responsive.py`** and **`src/modules/ui_metrics.py`**: Shared work-area fitting, dialog placement and event-driven monitor tracking. Keep Qt logical coordinates separate from Tk native pixels.
- **`src/modules/detector.py`**: USB serial port auto-detection and board identification helper.
- **`src/modules/downloader.py`**: Resumable multi-threaded downloader with SHA-256 validation and Google Drive virus-scan bypass.
- **`src/modules/reset_editor.py`**: Historical settings tool for older Tkinter releases. It does not switch or repair the current Qt editor; use the editor's Reload recovery control.
- **`src/modules/setup_ide_paths.py`**: Re-navigates `compile_commands.json` paths when sketches move across directories.

---

### Offline Monaco Editor & Web Assets

- **`src/editor/index.html`**: Host page for Monaco Editor with QWebChannel bridge polyfill, tab bar drag-and-drop, and CSS glowing animations.
- **`src/editor/bundle.js`**: Self-contained Monaco Editor offline JS bundle (no CDN dependency).
- **`src/editor/qwebchannel.js`**: Transport layer providing bidirectional Qt-JS object synchronization.
- **`src/editor/editor.worker.js` & `*.ttf`**: Web workers for syntax validation and bundled offline Montserrat/icon fonts.

---

### Caches, Installers & Reset Templates

- **`installers/`**: Bundled offline installers and drivers (tracked via Git LFS):
  - `CP210x/` — Silicon Labs USB-to-UART drivers.
  - `CH34x/` — WCH CH340 / CH341 USB serial drivers.
  - `arduino-cli.msi` — Arduino CLI installer.
  - `MicrosoftEdgeWebview2Setup.exe` — WebView2 installer.
  - `msys2-*.exe` — MSYS2 build tools.
- **`soft_reset/`**: Pre-configured minimal PlatformIO workspaces and dynamic templates for every supported microcontroller family (`soft_reset_project/` for ESP32, `soft_reset_project_uno/` for AVR, along with dedicated templates for ESP8266, STM32, RP2040, SAM/SAMD, Teensy, nRF52, and Renesas RA).
- **`.mcu_flasher_build_cache/`**: Generated build artifacts and isolated per-board workspaces (gitignored, hidden via Windows `attrib +h`).
- **`logs/`**: Runtime crash and diagnostic logs.

---

## ⚙️ Configuration

Application settings are persisted in `src/gui_config.json`:

```json
{
  "theme": "default",
  "baud_rate": 115200,
  "serial_port": "",
  "board": "esp32:esp32:esp32",
  "programmer": "esptool",
  "shared": {
    "cpu_multithreading": "HIGH",
    "graphics_acceleration": "ON",
    "reset_on_baud_change": false,
    "autosave_enabled": true,
    "autosave_delay": 2000,
    "auto_clear_serial_on_upload": true,
    "hide_build_console_warnings": false
  }
}
```

### Key Configuration Options:
- **`theme`**: Active UI color theme (`"default"` for Glass Smoked Dark, `"light"` for Glass Frosted Light, `"solarized_dark"` for Solarized Dark).
- **`cpu_multithreading`**: Compiler worker budgeting mode (`"LOW"`, `"MEDIUM"`, `"HIGH"`).
- **`reset_on_baud_change`**: Whether changing the serial monitor baud rate triggers a silent DTR/RTS hardware reset pulse (`true`/`false`).
- **`autosave_enabled` & `autosave_delay`**: Debounced automatic file saving state and delay in milliseconds.
- **`hide_build_console_warnings`**: Filter out non-fatal compiler warning lines from the build console.

---

## 🛠️ Development & Contributing

### System Requirements
- **Operating System**: Windows 10/11; native Ubuntu support is implemented, with Ubuntu 22.04/24.04 CI targets (native Linux verification pending).
- **Hardware**: Minimum **4 CPU cores** (physical topology when available, logical threads as fallback; enforced at startup)
- **Storage**: **6GB+** free disk space for toolchains, platforms, and compilers
- **Python**: Python 3.10+ required on machine when pulling/developing from source (pre-built release packages do **not** require Python on the machine)
- **Version Control**: Git with Git LFS (`git lfs install`)

### Verification & Syntax Checking

Run the hardware-free checks with the application's runtime. They use simulated
project data and check keyboard tabs, compact/wide window ownership, exact file
mapping, explicit detach/close/reattach, dirty renderer recovery, target rules,
CPU budgets and terminal controls. Rendered previews go under ignored `temp/`.

Settings preserves Solarized Dark as a manual choice while following the OS.
The downloader reads the same per-user theme policy and refreshes its palette
when reopened. Setup progress and status use the selected palette too. Settings
**Continuous panel resizing** controls splitter previews; GPU
policy is automatic. Its unsaved default is off on the constrained resource
profile. Saved user preferences take precedence. Settings writes once, reports
write failures and preserves unrelated configuration; reset actions recheck the
resolved target, framework, port and operation state before confirmation.

```powershell
& src/_python/python.exe -B direct/verify_runtime.py --render-dir temp/audit/workspace
& src/_python/python.exe -B direct/verify_runtime.py --preview-cpus 4
& src/_python/python.exe -B direct/verify_runtime.py --preview-cpus 6
& src/_python/python.exe -B direct/verify_terminal.py
& src/_python/python.exe -B direct/verify_performance.py
& src/_python/python.exe -B direct/verify_build_log_routing.py
& src/_python/python.exe -B direct/verify_build_console.py
& src/_python/python.exe -B direct/verify_platforms.py
& src/_python/python.exe -B direct/verify_offline.py
& src/_python/python.exe -B direct/verify_bootstrap_seed.py
& src/_python/python.exe -B direct/verify_bootstrap_updates.py
& src/_python/python.exe -B direct/verify_bootstrap_download_failure.py
& src/_python/python.exe -B direct/verify_bootstrap_native.py --report-dir temp/audit/bootstrap-cold
& src/_python/python.exe -B direct/verify_bootstrap_promotion.py --report-dir temp/audit/bootstrap-promotion
& src/_python/python.exe -B direct/verify_bootstrap_pip_paths.py
& src/_python/python.exe -B direct/verify_controls.py
& src/_python/python.exe -B direct/verify_board_search.py --render-dir temp/audit/board-search --benchmark temp/audit/board-search/timing.json
& src/_python/python.exe -B direct/verify_target_resolution.py
& src/_python/python.exe -B direct/verify_board_families.py
& src/_python/python.exe -B direct/verify_browser_loading.py
& src/_python/python.exe -B direct/verify_responsive.py
& src/_python/python.exe -B direct/verify_projects.py --render-dir temp/audit/projects
```

The cold-window and promotion checks use isolated configuration and simulated
dependency availability. They verify the original Qt design, retained logs and
reading position, held-scrollbar delay, warm startup and display-failure recovery
without running setup or installing packages.
Warm checks include Qt imported before window construction and retrying a
recoverable view failure, while still rejecting Qt loaded from another environment.

For an optional real ESP32 compile probe on Windows, use
`direct/verify_target_resolution.py --compile-installed-esp32`. It copies the
required installed packages into `temp/audit/target-resolution/`, builds a tiny
Arduino sketch, and verifies the application Compile button through board
resolution, `.ino` conversion, PlatformIO and `firmware.bin`. Persistence and
hardware calls are mocked; no upload runs and the live package store stays
unchanged. The copied packages use about 1.2 GB of scratch space.

`direct/verify_target_resolution.py --compile-installed-avr` proves real
Compile-button builds for Uno, Nano ATmega328 and Mega 2560 with isolated copied
packages and HEX outputs. `direct/verify_board_families.py` additionally checks
representative STM32, Pico, SAMD, nRF52, Teensy and unknown declared platforms,
framework selection/preparation, native Upload clicks without COM, transport
arguments, failed-build gating and UF2 caches using simulated processes. These
checks never install packages or write to physical hardware.

`direct/verify_platforms.py` checks host selection/import order, native package
paths, setup argument forwarding, upload process options and Bash/Windows Script
Host syntax without running installers or launchers. The compatibility workflow
also provisions AVR packages in a CI scratch store on Ubuntu 22.04/24.04, then
checks the actual Compile button for Uno, Nano and Mega using copied native
packages. Native Ubuntu results remain pending until that workflow runs.

For native setup/downloader screenshots, run the controls verifier with
`QT_QPA_PLATFORM=windows` and `--render-dir temp/audit/controls`. On Ubuntu use
`QT_QPA_PLATFORM=xcb` under a desktop or Xvfb. Setup classes are isolated from
installation and launch code; downloader fixtures never start network workers
or write live download settings. These checks cover all compact Actions,
settings persistence/signals, reset gating, theme switching and log trimming.
`direct/verify_hygiene.py` checks native Windows attributes, repeated hidden
metadata writes, failed-update preservation and user-file ownership in isolated
fixtures under `temp/`.
The responsive verifier covers narrow/wide control rows, readable baud values,
settings and setup on small work areas, wrapped board filters, popup scrolling,
monitor-change coalescing and native Tk details. Start a fresh process for each
`QT_SCALE_FACTOR` value (1, 1.25, 1.5, 1.75 and 2); the Tk fixture also simulates its
native point scaling. Couple physical screen dimensions to each scale, convert
to Qt logical coordinates once, and subtract taskbar space. Include half-monitor
and full-width workspaces, portrait screens and chosen content fonts; check the
effective rendered font, complete frames and readable viewports. Short setup
fixtures cover active packages, long statuses, hours on the clock and both logs.
Use `--render-dir temp/audit/responsive` for previews.
The project verifier checks competing processes, stale settings snapshots,
exclusive project/port claims, independent opening during compile, existing
folder protection and themed project-picker captures. It mocks GUI spawning
and metadata and writes only isolated fixtures under `temp/`.
`direct/verify_theme_readability.py` and `direct/verify_panel_readability.py`
check rendered text, retained output, terminal ANSI colors and notification
filter/refresh behavior across all three palettes. Notification persistence
checks in `direct/verify_notification_persistence.py` and
`direct/verify_notification_writes.py` use explicit temporary databases to verify
successful writes, denied replacements and preservation of existing history.
`direct/verify_bootstrap_headers.py` checks journal dividers, narrow-screen header
wrapping, failed-stage colors and restored native reading selection. Its optional
`--render-dir temp/audit/bootstrap-headers` produces dark/light/Solarized previews.
The runtime preview also checks all six tool panels in a 640-pixel workspace at the half-screen minimum,
including control visibility and the serial send bar. Its terminal layout probe
does not start a shell; `verify_terminal.py` checks the actual PTY separately.

Seed and update verifiers use isolated package metadata and fake installers.
The failed-download verifier uses an actual loopback HTTP 503 response and the
real downloader/failure callback, with an explicitly required seed and no
fallback installers. It captures whole-step red output and verifies that the
partial checkpoint is retained. This fixture is separate from a complete
production bootstrap run.

Ubuntu uses `.venv-linux/bin/python -B direct/verify_runtime.py`; see
[Ubuntu verification limits](direct/UBUNTU.md#verification-and-current-limits).
CPU previews simulate policy rather than physical device performance. Native
Windows WebView2 focus, Ubuntu desktop behavior and physical flashing also need
verification on their respective systems. Relevant `.agents/skills/` and the
application-only AGENTS generator document the same ownership/resource rules;
do not regenerate instructions against live caches during verification.

```powershell
# Verify syntax compilation across the PySide6 package entry point:
python -m py_compile main/mcu_flash_gui.py

# Verify syntax compilation across the backend bridge:
python -m py_compile main/web_bridge.py

# Verify syntax across all main Qt panels:
python -m py_compile main/qt/main_window.py
python -m py_compile main/qt/toolbar.py
python -m py_compile main/qt/editor_panel.py
python -m py_compile main/qt/serial_panel.py
python -m py_compile main/qt/terminal_panel.py
python -m py_compile main/qt/settings_dialog.py

# Launch GUI in development mode:
python mcu_flash_gui.py
```

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

> Made with ❤️‍🔥 by **Naph** — Happy flashing! 🚀
