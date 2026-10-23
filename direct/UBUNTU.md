# Ubuntu setup and platform behavior

Windows 10/11 keeps its downloaded Python in `src/_python` and its dependencies
in `env`. Ubuntu 24.04/26.04 uses native Linux launchers and its separately
prepared `.venv-linux`. Preserve both Windows directories when cleaning Ubuntu
or distributing one folder for both hosts. Linux runtime selection rejects
Windows `env` even if copied files resemble a Linux environment. Windows starts
through `LAUNCHER/WINDOWS.exe` or `direct/windows/run.vbs`, whose native path repair
updates a moved Windows environment before Bootstrap proceeds.

The application supports Windows and native Ubuntu 24.04 or newer. Windows
uses the bundled runtime and Windows launchers. Ubuntu uses a native
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
| `LAUNCHER/UBUNTU` / `LAUNCHER/UBUNTU_ARM64` / `src/launcher_folder_linux.c` | Compiled native amd64 and ARM64 launchers and their C source |
| `direct/ubuntu/architecture_guard.sh` / `src/modules/ubuntu_architecture.py` | Early 64-bit CPU admission and native ELF identity checks |
| `direct/ubuntu/arm_qt5.py` | ARM64-only access to Ubuntu's ABI-matched Qt5 sample-viewer package |
| `direct/ubuntu/launch.py` / `preflight.py` | First-run Bootstrap handoff, desktop launch and dependency diagnostics |
| `direct/ubuntu/install_desktop.py` | Local desktop shortcut and optional per-user Applications entry |

## Direct executable folder — 2026-10-23

Open `LAUNCHER` and double-click `UBUNTU` on AMD64 or `UBUNTU_ARM64` on ARM64.
These are native ELF executables and forward literal arguments to the same
Ubuntu Bootstrap path. `WINDOWS.exe` is the matching Windows GUI entry. No
`.desktop` file is required in `LAUNCHER`; retain Unix executable permissions
when copying. The older root entries and optional menu shortcut remain
compatible. See [the launcher guide](../LAUNCHER/README.md) and
[exact verification](../audit/LAUNCHER_EXECUTABLES.json). Other named OS entries
remain availability notices while their full application ports are pending.

## Native launcher correction — 2026-10-16

The application now contains compiled Linux executables under `LAUNCHER`: double-click
**`LAUNCHER/UBUNTU` on Intel/AMD** or **`LAUNCHER/UBUNTU_ARM64` on Raspberry Pi/ARM64**.
The earlier root Bash script did not provide the requested native executable.
Both binaries enter the existing Ubuntu Bootstrap path; the local desktop
shortcut is optional. The amd64 file is recognized locally as
`application/x-executable`. Source `afe091a` passed
[native ARM64 executable verification on both LTS releases](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/38836466685),
including fresh Bootstrap, warm GUI launch and ESP32 compilation; amd64 first-run
jobs passed too. These hosted checks do not replace physical Pi USB/graphics
verification. Windows executables and setup remain unchanged.

Ubuntu's private requirements constrain Click to `>=8.1.8,<8.2` while PlatformIO
uses a [parsing API deprecated in Click 8.2](https://click.palletsprojects.com/en/stable/changes/#version-8-2-0).
Runtime preflight rejects missing, malformed or incompatible Click metadata
before importing native GUI modules and sends existing installations through
Bootstrap repair. The actual missing-platform CLI check treats deprecations as
errors; warnings are not suppressed by this compatibility fix. Windows dependency
ranges are unchanged.

## Cloud and Fresh cleanup source update — 2026-10-15

Revision `7a58479` reuses the verified linked Cloud working folder, focuses an
already open copy without replacing buffers, and requires Replace/Cancel for
local name collisions. Confirmed replacement backs up affected root sources and
notes outside the project, preserves assets/settings/caches/AI journals, and
rechecks cloud revision, folder/link identity and source bytes before publication.
Rollback retains concurrent external edits and reports incomplete recovery.
Saved edits at legacy locations are preserved; existing duplicate folders are
not removed automatically. See the [Cloud guide](cloud/README.md).

The native Ubuntu fixture confirmed identical original/restored source bytes,
filenames, exact ESP32/Arduino settings, NimBLE-Arduino 2.5.1 configuration and
generated build inputs. Case-only renames, writable/read-only roots and failure
recovery were exercised. Host GCC wrote a dependency file through a 420-character
path. No Ubuntu cause matching the supplied Windows long-path compiler failure
was established, so native Ubuntu build-path handling remains unchanged.
The fixture's ESP32 compiler was mocked; this was not a full native NimBLE build.

Cloud service/configuration/UI, editor manifest, exact-board cache, runtime/Monaco
Pull and native PTY checks passed with mocked accounts, persistence and hardware.
Cloud layouts and the relevant responsive cases passed at 100/125/150/175/200%.
The 48-check native maintenance verifier passed before the requested Fresh clean;
cleanup then removed the generated environment and bytecode. The next launch
needs Bootstrap again. Logs and captures are under `temp/audit/ubuntu-cloud/`
and are excluded from source releases.

For this revision, CI's isolated native first-run jobs passed on Ubuntu 24.04
and 26.04, including full default preparation, actual workspace launch and a real
baseline ESP32 Compile-button build. The broader runtime jobs failed and their
downstream integration steps were skipped. These results do not certify the
Cloud NimBLE sketch, physical USB uploads, real Firebase/keyring integration or
native Wayland. See the [compatibility run](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/38670491891)
and [release notes](../RELEASE_NOTES.md) for packaging and verification limits.

## Native parity audit — 2026-10-13

The 2026-10-13 audit used Ubuntu 26.04.1 LTS (amd64), private Python 3.14.4,
PySide6 6.12.0 and native Tcl/Tk 8.6. Qt, WebEngine, Tk and PTY probes ran
under isolated Xvfb displays; the fake OpenCode CLI exercised actual capability
replies, Unicode, bracketed paste, hide/reveal, restart and process shutdown.
Cloud operations used isolated service/configuration fixtures, never real accounts.

The existing cloud Rename/Delete/Replace/copy actions, transparent Cloud surface,
static downloader glass, compact review/history and Syntax Check reflow were
validated. New native release admission covers launch, repair, bootstrap,
maintenance, direct GUI/downloader/sample-editor entries and native dependency
preparation. The obsolete old-Xft Fontconfig supplement was removed; the Qt ABI
check for custom Python 3.10/3.11 remains relevant on supported hosts.

Tk native resizing now retains the previously focused downloader control only
when X11 has no focused client; it refuses to force focus over another client.
Prompt merges preserve original observed text and all channel markers through
settling, pending journals and decision history. Combined AI Changes cards show
Mixed; unknown or legacy attribution remains Unknown. Existing journals are not
migrated. This is observed prompt context, not process-level write attribution.

Cloud service/configuration/UI, prompt-source, AI Changes, offline editor review,
recovery, runtime, performance, downloader appearance/workflow/preparation,
maintenance and native PTY/OpenCode renderer verifiers passed. The full responsive
suite passed at 100/125/150/175/200%; the changed Tk cases were rechecked at every
scale. Captures and logs are under `temp/audit/ubuntu-parity-2026-10-13/`.
Read-only `./LAUNCHER/UBUNTU --check` passes native host admission but reports missing
native tool/board preparation. No live installer, AI request, cloud mutation,
hardware operation or live-cache metadata generation ran. Windows-only checks
were skipped on this host. Current 24.04/26.04 CI, native Windows, real provider
integration, full live-store Bootstrap and physical hardware remain separate
verification requirements; this run used X11/Xvfb rather than a new Wayland audit.

## Install on Ubuntu

The native Resources browser, setup views and direct-link download form now use
colored frosted reading layers rather than black container fills, with soft
static reflections and rounded fields. Linux Tk loads bundled Montserrat only
inside its process through Fontconfig application fonts; system/user font files
are not changed. The file downloader supports resizing and keyboard-driven
scrolling. Appearance checks use isolated catalogs/forms at five scales; they
never install packages or start real downloads.

Use a normal desktop account and a writable checkout on 64-bit Intel/AMD or ARM64 Ubuntu 24.04 or newer.
All Ubuntu launch, setup, repair, downloader and direct Python entry points validate
the installed OS release before runtime preparation, installation or services.
Older releases stop with “MCU Flasher requires Ubuntu 24.04 or newer.” Missing,
malformed or non-Ubuntu OS release information stops explicitly. Rejection performs
no cleanup or migration and preserves settings, credentials, projects and resources.
The Bash check also runs before missing-system-Python rescue; it never sources
os-release as shell code. Python admission reads native freedesktop release data.
`direct/verify_ubuntu_release.py` uses isolated fixtures for older, minimum, newer
and malformed releases; older fixtures verify rejection only.
Python 3.10 or newer,
four CPU cores, and enough space for native board toolchains are
required. Plan at least 10 GB free for the default AVR/ESP32/ESP8266 Arduino
plan, with 20 GB recommended; these are estimates, not measured peak usage or
enforced thresholds. See [system requirements](../README.md#system-requirements).
Ubuntu 24.04 and 26.04 are the compatibility workflow targets.
The first-launch job uses each release's system Python, including Python 3.14
on 26.04. Check the completed compatibility run for the revision being used;
first-launch, runtime and compile results are separate jobs. CI uses X11/Xvfb
and does not replace on-device Wayland, keyring or USB checks.

Double-click `LAUNCHER/UBUNTU` on Intel/AMD, or `LAUNCHER/UBUNTU_ARM64` on Raspberry
Pi/ARM64. Bootstrap detects missing desktop
dependencies, opens a setup terminal and asks for Ubuntu administrator
authentication. It refreshes package indexes and installs the detected packages,
then prepares the private runtime and opens the app automatically. You do not
need to type apt commands. Internet access and an administrator account are
needed when packages are missing.

For a dependency reset, preview `python3 cleaner/ubuntu/maintenance.py runtime`
and add `--apply` to carry out the reviewed plan. It removes the validated
`.venv-linux`, owned native tools and Linux offline extras, preserving sketches,
settings and protected build/recovery caches. The shared XDG PlatformIO store
requires the separate `--include-native-store` opt-in and matching ownership
receipt. Packaged library/directory aliases must resolve inside that exact
architecture tree and are unlinked without following them. External, broken or
cyclic links still refuse cleanup. Next launch uses Bootstrap to download or
restore missing dependencies;
do not include generated environments or audit fixtures in source releases.

For **Fresh** dependency cleanup (the environment and source bytecode), preview
`bash cleaner/clean_fresh.sh`; apply the reviewed plan with
`bash cleaner/clean_fresh.sh --apply` and its confirmation prompt. This preserves
user settings, credentials, sketches and protected recovery/build caches; it
does not reset the desktop user's account. Python's exact generated root
`.gitignore` is accepted inside `.venv-linux`; custom files and unexpected links
still refuse cleanup. See the [maintenance guide](../cleaner/README.md).

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

Qt also needs ALSA and GLib: install `libasound2t64` and `libglib2.0-0t64`
on supported Ubuntu releases. Bootstrap uses these package names. See Qt's
[Linux requirements](https://doc.qt.io/qtforpython-6/overviews/qtdoc-linux-requirements.html)
for other distributions or missing system libraries.

Ubuntu 26.04's GNOME desktop uses Wayland and retains XWayland for X11 apps.
Bootstrap accepts either desktop display and prepares both clipboard backends.
The CI renderer checks currently use X11 through Xvfb; they do not prove native
Wayland behavior. See the [Ubuntu 26.04 release notes](https://documentation.ubuntu.com/release-notes/26.04/summary-for-lts-users/#wayland-session).

From the project directory:

```bash
# Intel/AMD: double-click the supplied native executable, or run:
./LAUNCHER/UBUNTU
# Raspberry Pi/ARM64: double-click the matching native executable, or run:
./LAUNCHER/UBUNTU_ARM64
# Check prerequisites/readiness on either CPU without setup or GUI:
bash direct/ubuntu/run.sh --check
# Register an Applications menu entry and generate the local desktop shortcut:
bash direct/ubuntu/run.sh --install-shortcut
# Rebuild this CPU's native executable (requires build-essential):
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

`LAUNCHER/UBUNTU` is the compiled native amd64 executable; `LAUNCHER/UBUNTU_ARM64` is
the compiled native ARM64 executable for Raspberry Pi and other ARM64 computers.
Use the file matching the host CPU. Keep `LAUNCHER` beside `direct/`, `main/` and
`src/`; the application still needs those folders and its runtime/toolchains.
Executable permissions are supplied; if a copy loses them, use
`chmod +x LAUNCHER/UBUNTU LAUNCHER/UBUNTU_ARM64`. A filesystem
mounted with `noexec` can use `bash direct/ubuntu/run.sh` to enter setup, but the
private runtime and native tools still need executable storage. The generated
folder shortcut resolves the application beside the shortcut at click time and
invokes Bash, so its launch path survives a folder move and lost execute
permissions on launcher scripts. Keep that shortcut beside `direct/`, `main/`
and `src/`. Ubuntu may require right-clicking it and choosing **Allow Launching**.
The optional Applications menu
entry uses absolute paths; after moving the folder, run
`bash direct/ubuntu/run.sh --install-shortcut` to regenerate that menu entry.

### Application icon

The raw native executables may show Ubuntu Files' generic executable/gear icon.
Generate the branded folder shortcut and Applications entry from the application
folder, without sudo:

```bash
bash direct/ubuntu/run.sh --install-shortcut
```

Both shortcuts use the existing MCU Flasher artwork. Registration copies the
256px PNG extracted unchanged from `src/assets/mcu_icon.ico` into
`${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps/mcu-flasher.png`.
The themed icon name stays valid after a folder move on the same account.
Generate shortcuts once on each new computer/account; refresh the fixed
Applications entry after moves. The generated folder shortcut may require
**Allow Launching** before Ubuntu displays and launches it as an application.
The running Qt application uses the matching `mcu-flasher` desktop identity;
the X11 entry also declares its measured window class.

The icon update needs no executable reset, runtime repair or recompilation.
The public wrappers load the current Python application from the parent of
`LAUNCHER`. Keep that folder together with `main`, `src` and `direct`.

`python3 direct/build_launcher_folder.py` rebuilds the matching host's public
entries from `src/launcher_folder_linux.c` into `LAUNCHER`. Its `--output`
option accepts another existing output folder. The supplied executables and
Bash fallback need no compilation on the user's computer. The older
`direct/ubuntu/build_launcher.sh` remains a developer fixture for the legacy
root-wrapper source; it is not the public launcher builder.

## Raspberry Pi and ARM64

The Ubuntu branch selects `amd64` for `x86_64`/`amd64` and `arm64` for
`aarch64`/`arm64`. Raspberry Pi 5 uses ARM64. Install the **64-bit Ubuntu Desktop
24.04 LTS or 26.04 LTS image** for your model; a desktop session and four CPU
cores remain required. Ubuntu lists desktop support for Pi 4/5 and related
models with at least 4 GB RAM. 8 GB gives the editor and compiler more headroom.
Check [Ubuntu's support and EEPROM requirements](https://ubuntu.com/hardware/docs/boards/how-to/ubuntu_supported/raspberry-pi/)
before installing the OS. A 32-bit userspace, older 32-bit Pi, Raspberry Pi OS,
or a server installation without a desktop does not meet this setup's contract.

Launch exactly as on an Intel/AMD Ubuntu machine:

```bash
bash direct/ubuntu/run.sh
# Diagnose only, without installing anything:
bash direct/ubuntu/run.sh --check
```

Bootstrap uses ARM64 PySide6/WebEngine wheels for the workspace. The separate
read-only sample viewer uses Ubuntu's `python3-pyqt5`, `python3-pyqt5.qsci` and
`python3-pyqt5.sip` packages because upstream PyQt5 has no Linux ARM64 wheels.
Use Ubuntu's `/usr/bin/python3` when provisioning manually so its Python ABI
matches these packages. Bootstrap exposes only their `PyQt5` namespace through
a private-environment import hook; it keeps system site packages disabled and
runs both viewers from `.venv-linux`. Qt5 and Qt6 remain in separate processes.
The amd64 wheel requirements and Windows dependencies retain their existing paths.

ARM64's Qt WebEngine wheels also use Ubuntu's media/compression libraries,
including Opus, LittleCMS, Minizip, Snappy and WebP. Bootstrap detects these before
private setup; its ARM-only prerequisite list follows the reviewed Qt wheel ELF
dependency closure. Runtime checks validate CPU identity and all linked
libraries before importing Qt, so diagnostics list missing SONAMEs together.
To print the complete manual apt command for the current host without installing:

```bash
/usr/bin/python3 -B -c 'from direct.ubuntu.preflight import system_install_command; print(system_install_command())'
```

Arduino CLI and OpenCode downloads select pinned, checksum-verified native
ARM64 assets. Receipt and ELF checks reject tools copied from the other CPU.
Native PlatformIO stores and firmware receipts already separate the host
architecture; upstream packages must also provide tools for that host. A board
package without Linux ARM64 binaries cannot be made ready by using an amd64 tool.

When moving an application folder between Intel/AMD and ARM64 Ubuntu, run
`bash direct/ubuntu/run.sh --repair` on the destination. Bootstrap detects a
foreign private-runtime receipt or native extensions and reinstalls the Ubuntu
requirements for the current CPU, then prepares that CPU's board store. Preserve
the Windows runtime, settings, sketches and protected caches when moving it.

`.github/workflows/ubuntu-arm64.yml` adds native Ubuntu 24.04/26.04 ARM64 jobs
for bootstrap contracts, the real Qt/Monaco and sample viewers, PTY, resource and
responsive checks, plus empty-store preparation and an ESP32 compile fixture.
Adding these jobs is not evidence that they have passed. Local amd64 architecture
fixtures simulate CPU selection and wrong-CPU rejection; they do not certify
execution on a physical Pi, Wayland, keyring or USB upload/reset behavior.

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

Use the matching Linux `LAUNCHER/UBUNTU` / `LAUNCHER/UBUNTU_ARM64` or Bash entry. The `.exe`, bundled Windows Python and
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
launch opens the available Ubuntu terminal with live Bootstrap output.
Ubuntu 26.04 and newer prefer installed **Ptyxis**, the new Ubuntu terminal,
using `--standalone --` with literal command arguments. If it is unavailable,
the existing terminal alternatives remain available. Ubuntu 24.04 keeps the
existing selection order. This also covers missing-system-Python recovery;
setup invoked inside an existing terminal continues in that terminal. See the
[Ubuntu 26.04 desktop changes](https://documentation.ubuntu.com/release-notes/26.04/summary-for-lts-users/).
Failed setup stays visible; success opens the workspace and closes that setup terminal.
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
The workspace uses PySide6/WebEngine binary wheels on amd64 and ARM64 to avoid
an unattended Qt source build. The separate sample viewer uses PyQt5/QScintilla
wheels on amd64 and Ubuntu's ABI-matched Qt5 packages on ARM64 through the narrow
private-environment import hook described above. Run the GUI as your desktop
account; do not disable Chromium's sandbox or run it through sudo.
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
CLIs. Their output scrollbar is hidden while mouse-wheel and keyboard
scrolling retain access to scrollback. Output is acknowledged after xterm parses it, so a slow renderer applies
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
Shiboken wraps private Qt/WebEngine objects. Pointer filtering uses this owned
widget scope on Windows too. Native Build/Serial Clear also keeps
retained text cursors alive until Qt finishes clearing its document, avoiding a
cursor use-after-free during synchronous scrollbar callbacks. The retained
text-cursor workaround is Linux-only.

Serial reader failures retry at bounded delays, retain port selection, and
stop after repeated failures. Stale readers cannot disconnect newer sessions.
Permission failures need user correction. Editor renderer failures preserve
dirty source buffers in process memory and reload a bounded number of times;
a visible reload control remains available after that budget is exhausted.
Each renderer has a distinct recovery token. The replacement loads its initial
project once and acknowledges recovery only after checking the restored model
text; delayed work from an older page cannot clear its buffers. Explicit project
reload and cloud Pull still load the newly selected sources.
Cloud Pull first certifies its source list in a worker; failed directory scans
cannot masquerade as an empty sketch. The complete backend source list must have
mapped, live Monaco models and successful reads. Completion must come from the
renderer that
started the refresh. A failed or interrupted refresh retains the protected editor
and Retry action instead of reporting success with missing or stale tabs.
Cloud connection and upload prompts accept native mouse input, scroll their
forms on short screens and retain visible actions as the work area changes.
This does not recover from complete process termination. Flashing, erasing,
resets, and shell commands are never automatically replayed by these recovery
paths. Errors remain visible; broad try/catch blocks do not prove recovery.

## Verification and current limits

### ARM64 addition — 2026-10-15

Local amd64 verification completed with 400 checks passed and three native
Windows checks skipped. The isolated checks cover CPU selection, wrong-CPU
runtime/tool rejection, ARM64 dependency routing, the then-current Bash launcher, Bootstrap,
maintenance and Windows setup/runtime boundaries. ARM64 fixtures simulate host
identities and providers; native ARM64 and native Windows execution were not
performed in those local fixtures. The first native ARM64 CI run on `90a45ed`
failed before workspace startup on both LTS releases because `libopus.so.0` was
missing. The follow-up adds the complete reviewed ARM Qt dependency mappings.
Corrected source `03c6703` then passed the entire
[native ARM64 run](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/38750157321)
on Ubuntu 24.04 and 26.04: private runtime and host contracts, actual Qt/Monaco
workspace, Qt5 sample viewer, native PTY, performance and responsive checks,
empty-runtime/store Bootstrap, prepared relaunch and a real ESP32 Compile-button
build. Both hosts produced 267,888-byte firmware; neither performed a USB upload.

Offscreen responsive layout checks passed. The native xcb responsive run
still reports a setup-dialog frame-clamping failure that also reproduces on the
unmodified baseline, so this addition does not claim a full native responsive
suite pass. Physical Raspberry Pi USB upload/reset, native Wayland and real
provider/keyring integration remain separate checks. The dated audit results
below describe their recorded revisions and environments.

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
.venv-linux/bin/python -B direct/verify_ubuntu_architecture.py
.venv-linux/bin/python -B direct/verify_ubuntu_arm_bootstrap.py
.venv-linux/bin/python -B direct/verify_ubuntu_arduino_cli.py
.venv-linux/bin/python -B direct/verify_ubuntu_opencode_setup.py
.venv-linux/bin/python -B direct/verify_ubuntu_download_manager.py
.venv-linux/bin/python -B direct/verify_ubuntu_resets.py
.venv-linux/bin/python -B direct/verify_ubuntu_pty_lifecycle.py
.venv-linux/bin/python -B direct/verify_qt_binding_lifetime.py
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
fixture with an empty runtime and native store, runs the Ubuntu launch path
and the full default Bootstrap, opens the actual Monaco workspace, verifies a prepared
launch, clicks Compile for ESP32 and runs the runtime checks. It redirects home
paths in the fixture and prohibits serial opens; installers run normally only
inside the fixture. It leaves logs and firmware there and never installs system
packages. CI runs this separately on Ubuntu 24.04/26.04; its explicit
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

The existing Ubuntu compatibility CI job additionally provisions native AVR
packages under `temp/`, then uses copied packages to compile Uno, Nano and Mega through the application's
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

On 2026-10-11, Ubuntu 26.04.1 with Python 3.14.4 and PySide6 6.12.0 passed
the exact `31e6e24` runtime fixture (37 checks and real Monaco recovery).
The subsequent Cloud Pull and prompt fixes passed 46 runtime/manifest checks and three
detach/reattach/reload cycles on both the GNOME XWayland and native Wayland
backends. Each cycle retained active and inactive Unicode dirty buffers; a
confirmed Pull then replaced every source model. Native XWayland cloud UI
and developer UI fixtures passed 25 and 18 checks respectively.
The isolated performance suite passed all 26 checks, including real Node editor
resource/debounce checks, bounded logs and background parsing.
All 16 responsive Qt/Tk checks passed at 175% and 200% on an isolated native
Xvfb display, with saved fonts and negative-origin assertions retained. The
real GNOME compositor auto-maximized oversized synthetic fixture windows;
its forced sizes are unsuitable for that suite's artificial work-area cases.
Actual Wayland cloud prompt checks passed native mouse routing, complete frame
containment and input visibility in all three palettes. Shared fitting measures
native frame margins, including decorations that appear after the first resize,
and adjusts minimum sizes without changing content fonts. Tab-scroll arrows
use the selected palette and retain left/right click behavior.
Their cached normal, active and disabled glyphs survive native Qt standard-icon
assignment during tab layouts, including Windows styles used by CI.
The xterm viewport hides only its scrollbar, preserving overflow, terminal
history and wheel scrolling; session-tab navigation remains available.
`direct/verify_terminal_scroll.py` checks both actual offline renderer pages
with fixture transports, real wheel/key events and no shell or account access.
Cloud prompt fixtures also force overflow from measured content and frame
heights, because offscreen windows have smaller decorations and can fit forms
at heights that require scrolling on Wayland. Fixed monitor cases and complete
field visibility, text and font assertions remain in place.
The standalone browser-loading suite passed 29 checks on Python 3.14.4.
Cloud service, owner-ticket service and provider configuration fixtures passed
38, 27 and 10 checks. Isolated launch, Bootstrap, Offline Mode, platform and
PTY cleanup fixtures passed 107 checks, with two Windows-only cases skipped.
The GNOME Secret Service passed an isolated store/read/remove round trip using
the production vault adapter and a unique test-only service namespace.
No existing account record or real sketch was accessed by those fixtures.
At that audit, the host still needed normal Bootstrap preparation: `.venv-linux`
was absent and the read-only launcher check reported missing `libsecret-tools`.
Verification used a disposable runtime and checksum-verified temporary Ubuntu vault tools;
it did not install system packages or provision live board stores. At the
user's request, generated Linux/Windows Python runtimes and bulky isolated
audit dependencies were subsequently removed. Compact validation evidence and
the original settings, installers, sources and protected caches were retained;
the checkout is approximately 1.5 GB, with nearly 1 GB in Git history. The
receipt-authenticated XDG native PlatformIO downloads were also removed,
freeing approximately 5.1 GB outside the checkout. All 45 isolated Ubuntu
maintenance checks passed, including external-link and other-architecture
preservation; CI runs this verifier on each Ubuntu runtime combination.

Earlier Ubuntu 24.04 amd64 verification passed with the native xcb backend on an
isolated Xvfb display: 35 runtime checks plus the real Monaco workspace, seven
event-filter/heap-crash checks, 25 native serial-view checks, 24 build-console
checks, and 26 performance checks. The responsive suite passed 14 Qt/Tk checks
at 100/125/150/200% with PySide6 6.8.3 and again at 100% with 6.12; current
runtime/renderer/crash checks also passed with 6.12 on newer Python. An isolated
native binding probe subsequently reproduced sustained `None` reference loss
with Linux PySide6 6.12.0 on Python 3.11; all nine cases pass with 6.11.2.
Ubuntu therefore uses `PySide6<6.12` on Python 3.10/3.11 and retains `<7` on
Python 3.12+. Readiness rejects the incompatible combination before opening
the workspace and directs existing installations through Bootstrap repair.
The clipboard verifier needs a native xcb display through
process exit; the offscreen plugin can crash while releasing transferred MIME
data after its assertions pass. Ubuntu CI runs that fixture under Xvfb.

The early verification host lacked `libxcb-cursor0` and `libxcb-xinerama0`; validation supplied
official Ubuntu libraries only in temporary process-scoped fixtures. Normal
launch now hands those packages to automatic Bootstrap with administrator
authentication. The isolated system-setup suite verifies successful installation,
cancellation, apt failures, dependency rechecks, private-runtime continuation
and missing-Python rescue with fake installers. Those UI/system checks did not
run installers or firmware operations; separate isolated native first-run and
ESP32 Compile probes are described below. Other Wayland desktop environments
and actual hardware remain separate checks.
Windows driver installers and UNC mappings remain Windows-specific. The root
`LAUNCHER/UBUNTU` and `LAUNCHER/UBUNTU_ARM64` now supply the native Ubuntu amd64 and
ARM64 executables, sharing the existing Bash Bootstrap path.

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
