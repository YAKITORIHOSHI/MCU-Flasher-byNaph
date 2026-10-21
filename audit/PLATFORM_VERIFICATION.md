# Current application platform verification

22 October 2026, Asia/Manila. This records the shipping Python application;
the [native Rust replacement requirements](migration/PLATFORM_SUPPORT.md) are
tracked separately. AMD means x86-64, including compatible Intel processors;
ARM means ARM64. A launcher, cross-platform toolkit or passing editor fixture
does not establish a complete working application on another operating system.

| Requested system | Current application status | Evidence and remaining qualification |
| --- | --- | --- |
| Windows 10/11 AMD64 | Existing production entry and backend | Hosted AMD64 browser/editor rendering passes in two Python/Qt ranges; final launcher/archive/resource checks also pass. Hosted Server images do not establish physical Windows 10/11, standard-user/UAC, mixed-monitor DPI or real USB driver/upload behavior. |
| Windows 10/11 ARM64 | Unavailable | New `LAUNCHER/WINDOWS.vbs` refuses this host. Python repair/release still selects AMD64, CP210x setup selects x64 for any 64-bit host, and native ARM64 terminal/runtime/USB closure remains unqualified. Editor-only ARM checks are separate. |
| Ubuntu 24.04 LTS AMD64/ARM64 | Existing production entries and native host policies | Both native CPU families and 64-bit userspace are admitted. Existing CI covers runtime/first-run paths; this performance slice adds focused checks per release/CPU. Xvfb and software-rendered fixtures do not establish actual Wayland/X11 input, compositor or physical USB behavior. |
| Ubuntu 26.04 LTS AMD64/ARM64 | Existing production entries and native host policies | Focused native browser/editor/PTY checks pass on both hosted CPU families using the release's own Python/Tcl/Tk ABI and native ARM Qt dependencies. Local 26.04.1 AMD64 evidence also exists. Physical desktop, input and USB qualification remain separate. |
| macOS Intel/Apple Silicon | Unavailable | No Darwin backend, private runtime selection, native PTY/process containment, keychain or app bundle exists. Native offline editor checks alone cannot qualify these services. |
| Fedora x86_64/aarch64 | Unavailable | Production release validation intentionally rejects Fedora before Ubuntu setup/repair. A Fedora package/dependency, process, credentials and desktop adapter is required. |
| Linux Mint AMD64 | Unavailable | Exact Ubuntu release validation rejects Mint even when `ID_LIKE` mentions Ubuntu. Its own reviewed admission/setup and desktop qualification are required. LMDE and any ARM port need separate policies. |

## Source evidence

- `main/platforms/__init__.py` selects only Windows and Linux backends.
  `main/mcu_flash_gui.py` checks Ubuntu admission/private runtime before Qt.
- `src/modules/ubuntu_release.py` and `direct/ubuntu/release_guard.sh` require
  exact `ID=ubuntu` and a valid version at least 24.04; missing/malformed release
  data refuses. `direct/ubuntu/architecture_guard.sh` admits x86_64/amd64 and
  aarch64/arm64 with 64-bit userspace.
- `direct/ubuntu/setup.py` validates before changing the runtime.
  `system_setup.py`, `preflight.py` and `arm_qt5.py` retain Ubuntu package/ABI
  policies, including Qt6/WebEngine and the separate Qt5/QScintilla process.
  No generic Fedora/Mint installer is connected.
- `src/modules/private_python_guard.py` and `src/modules/bootstrap.py` still
  select `python-*-amd64.exe` for Windows repair. `direct/build_release.py`
  validates Windows PE machine `0x8664`. CP210x setup chooses its x64 installer
  from a generic 64-bit check. ARM Node/Arduino CLI selectors are only subsets.
- `main/platforms/ubuntu_pty_process.py` rejects non-Linux; its supervisor uses
  Linux process containment. `credential_store.py` provides Windows vault and
  Linux `secret-tool`, with no macOS keychain provider.

## Checks for this performance slice

The focused browser workflow covers Windows AMD64 in two Python/Qt ranges and
Ubuntu 24.04/26.04 on both CPUs. It uses owned catalogs, sockets and fake launch
targets, software-rendered pages and display fixtures. No live settings,
metadata, installer, firmware command, serial write or upload/reset runs.
Separate **Editor component availability** jobs target Windows 11 ARM64,
macOS 15 Intel and macOS 15 Apple Silicon. They first import the actual native
WebEngine bindings. Available bindings run the offline Monaco/xterm pages;
unavailable Windows ARM bindings record the gap and skip rendering explicitly.
The current official ARM64 PySide6-WebEngine 6.12.0.140 wheel has no WebEngine
native modules. The associated Addons/Essentials wheels also lack them. See the
[official package artifact](https://pypi.org/project/PySide6-WebEngine/6.12.0.140/).
None of these jobs launches the app or changes its platform admission. The
three-core hosted Apple Silicon machine is also below the application's current
four-core minimum.

The initial focused run
[39602608777](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39602608777)
at `7c128ca` passed Ubuntu 24.04 AMD64. Windows found native-event timing gaps
in the new resize fixtures; Ubuntu ARM's browser checks passed but its Qt
resource checks lacked `libopus.so.0`. Corrections use the native event loop
and the production architecture-specific dependency list. Retain that failure
receipt separately from the corrected run; it is not an all-platform pass.

The existing `compatibility.yml` and `ubuntu-arm64.yml` define broader runtime,
first-run and compile jobs. Their source alone is not a passed-run receipt.
Exact successful runs remain linked from README and the migration handoff.
Fedora and Mint desktop execution is not recorded in this slice.

The expanded run
[39648393272](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39648393272)
at `c8a1377` passes all four Ubuntu 24.04/26.04 AMD64/ARM64 browser jobs and the
Intel/Apple Silicon macOS 15 offline editor component jobs. Every rendered host
checks five display scales, Arduino syntax and red/green AI decorations.
Windows 11 ARM records the missing WebEngine binding and explicitly skips
rendering; its green availability job is not an ARM rendering pass. Both AMD64
Windows jobs fail a newly exercised WSH-to-WSH argument boundary; runtime
preflight and owned timeout cleanup pass. Preserve that failure alongside the
subsequent Windows-only correction and rerun. No macOS application support or
Fedora/Mint execution is implied by this evidence.

The Windows-only run
[39650299818](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39650299818)
at `2b79f98` passes both complete native rendering steps, including Tk browser
appearance/performance, five embedded viewport scales, Arduino syntax and AI
red/green review colors. Actual portable entry execution with no arguments and
moved project paths passes. The run remains failed because two new verifier
assumptions are wrong: a seeded VBScript class shares its variable's name, and
a supposedly fitting label overflows the Windows font. The corrections at
`3331153` change fixtures only; no additional platform support is implied.

At `3331153`, the focused run
[39652819135](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39652819135)
passes both Windows launcher suites (15 executed / 9 platform skips each) and
release-source policy (7/7 each). The seeded literal argument route now passes.
The overall run remains failed because the larger-font fixture's replacement
label also overflows its measured edit field; this does not establish a
production marquee failure.

The final focused run
[39653405199](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39653405199)
at `91632a1` passes both Windows jobs: launcher 15 passed / 9 platform skips,
release policy 7/7 and Qt resources/marquees 32/32 each. The fitting-label fixture
now selects a nonempty sample using the current native font metrics before
independently asserting both overflow transitions. This run explicitly skips
the native painting step already passed at `2b79f98`; product source did not
change. The earlier Ubuntu/macOS renderer source hashes also remain unchanged.
Neither a skipped check nor a partial host job is counted as full application
qualification on a pending platform.

Runner availability and limitations were checked against the official
[GitHub runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
Windows hosted VMs run as Administrator with UAC disabled; macOS ARM standard
runners have three CPUs. Keep these limits explicit during release qualification.
