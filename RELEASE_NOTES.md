# MCU Flasher by Naph — Release Notes

> **Version:** v10 — Stable Release<br>
> **Release Codename:** *Quantum Bridge & Core Shield*<br>
> **Target Platforms:** Windows 10/11 (64-bit) · Native Ubuntu 24.04 or newer (amd64 / ARM64)<br>
> **Architecture:** Native PySide6 (Qt) · Offline Monaco · Asynchronous WebBridge<br>
> **Toolchain:** PlatformIO / SCons · Selective Arduino CLI support<br>
> **GitHub Release Published:** October 6, 2026<br>
> **Notes Updated:** October 15, 2026<br>
> **Cloud Verification Baseline:** [`7a58479`](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/commit/7a584794f9c9028e921b57c719c9645e8eb9748d)

---

> **Source update and download status:** These notes include the October 15 source fixes. As checked on October 15, the [published Windows ZIP](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/releases/tag/latest) was last updated on October 14, and the `latest` tag points to `e68e813`, an earlier revision. Rebuild and verify the release assets before describing these new fixes as included in that download. The verification limits below still apply.

## October 15 Source Update: Ubuntu ARM64 and Raspberry Pi

Ubuntu's existing launch and Bootstrap path now selects **amd64/x86_64** or
**ARM64/aarch64**. Raspberry Pi 5 and other supported Pis can use the same
application folder with **64-bit Ubuntu Desktop 24.04 LTS or 26.04 LTS**, a desktop
session and at least four CPU cores. Older 32-bit Pi models and Raspberry Pi OS
remain outside the supported setup. See the [Ubuntu setup guide](direct/UBUNTU.md#raspberry-pi-and-arm64).

- **One Ubuntu entry:** `MCU_Flasher` is a portable Bash launcher for both CPUs.
  Optional native C builds use separate `MCU_Flasher-amd64` / `MCU_Flasher-arm64`
  filenames and preserve the portable entry and Windows executables.
- **Native dependencies:** ARM64 uses PySide6/WebEngine wheels and Ubuntu's
  release-matched Qt5/QScintilla/SIP packages for the separate sample viewer.
  Only the PyQt5 namespace is exposed to the private runtime; broad system site
  packages stay disabled. The amd64 Qt5 wheel path stays available.
- **Correct tools after a move:** Arduino CLI and OpenCode select pinned,
  checksum-verified native archives. ELF CPU checks and architecture receipts
  reject foreign tools and detect private runtimes copied between amd64 and
  ARM64. Run Ubuntu `--repair` on the destination computer.
- **Windows preserved:** This ARM64 change leaves Windows production code,
  launchers, dependency setup and executable unchanged.
- **Verification:** Local amd64 verification passed **400 checks**, with
  **3 native-Windows-only checks skipped**, plus a real Qt WebEngine/PTY probe
  using a local fake CLI. All 18 offscreen responsive layout checks pass.
  One native setup-frame fixture has six failing short-work-area cases that
  also reproduce with the original verifier and unchanged UI source.
- **Native ARM64 CI added:** Ubuntu 24.04/26.04 ARM64 jobs cover runtime,
  sample viewer, PTY, responsive checks, empty-store Bootstrap and a real ESP32
  Compile-button fixture. These new jobs have not yet been verified for this
  source update. Local CPU simulations do not certify execution on a physical
  Pi, USB upload/reset, native Windows or every vendor's ARM64 board packages.

This source update does not rebuild or replace the published release ZIP.
Earlier CI results below remain scoped to their recorded source revisions.

## A More Expressive Workspace, With More Reliable Board Preparation

MCU Flasher v10 refines the glass workspace for editing, compiling, flashing, and monitoring firmware. This update adds original SVG artwork, completes the Developer Access redesign, restores the Library & Board Browser's glass appearance, and improves setup and recovery on Windows and Ubuntu.

Board preparation preserves exact target identities and reports missing or ambiguous support. Editing, serial monitoring, and build output remain responsive during background work.

**Existing users should run Bootstrap Repair while online after updating.** This checks runtime dependencies, the configured package plan, and board-readiness records before offline use.

## October 15 Source Update: Safer Cloud Sketches

- **Reuse the working project:** Opening the same verified account/provider/sketch reuses its linked folder and saved sources. If it is already open, MCU Flasher focuses that window without pulling files or replacing editor buffers.
- **Explicit updates:** Use **Pull latest** to confirm an update of the linked project. Reopening a sketch does not discard local edits.
- **Clear local collision choices:** A same-name unlinked project or another sketch from the same account requires **Replace** or **Cancel**. The prompt names the exact destination. Downloads no longer silently create numbered local folders. **Save a copy** still creates a separately named remote sketch when uploading a duplicate name.
- **Protected replacement:** Replace backs up affected root sources and notes outside the project, restores the verified cloud snapshot, and removes only obsolete root sources/notes. Assets, settings, build caches and AI journals are preserved. Another account/provider's linked folder and already open target workspaces are protected.
- **Safer failure recovery:** Cloud revision, folder/link identity and source bytes are checked again after confirmation and staging, before replacement. If storage fails or another process edits the files, recovery retains concurrent edits and reports any backup still needed for manual recovery.
- **Preserved older working copies:** Location adoption keeps saved edits and focuses an already open verified copy. Existing user duplicate folders are not removed automatically.
- **Source-only archives:** Only root `.ino`, `.cpp`, `.c`, `.h`, `.hpp` and `.txt` files travel through Cloud. Toolchains, libraries and build caches are prepared separately. An isolated Ubuntu fixture verified identical source bytes and filenames for a synthetic sketch before and after the cloud round trip, with the same exact board, Arduino framework, library version and generated build inputs.
- **Native Ubuntu checks:** Case-only filename changes, permissions, cancellation, retries, partial writes, rollback and recovery reporting were exercised on a case-sensitive filesystem. No Ubuntu cause matching the supplied Windows long-path failure was established; native build-path handling remains unchanged.

Source, network and credential work stays off the Qt thread. Existing save/busy protections, pending AI reviews, independent project windows and all editor models remain protected during Cloud updates. See the [Cloud guide](direct/cloud/README.md).

## Original SVG Icons and Theme Improvements

- **Utility glass refinement:** Resources, setup and the direct-link downloader use colored frosted reading surfaces with soft reflected light in place of black container fills. Native Ubuntu Tk uses the bundled Montserrat fonts without installing them system-wide. Rounded fields preserve native editing, and compact download forms keep their focused actions reachable by scrolling. This is a later source update; the published Cloud CI baseline below does not certify these visual changes.
- App interface emoji have been replaced with original SVG icons across the workspace, setup, Resources browser, Developer Access, and supporting controls.
- Icons use colors suited to **Glass Smoked Dark**, **Glass Frosted Light**, and **Solarized Dark**, including selected, disabled, and hover states.
- The early setup interface and Qt workspace share the same local SVG designs. Icons do not depend on an internet connection or the operating system's emoji font.
- User sketches, serial output, terminal text, and ticket content retain their Unicode text.
- The Library & Board Browser again uses smooth glass reflections, softer borders, rounded controls, circuit artwork, and a themed empty state. Reflections remain static.
- Browser selection colors now update correctly when switching themes, and compact headers preserve room for saved reading fonts.
- The main toolbar's **Boards_Libraries** button is now **Resources** on Windows and Ubuntu.
- Windows Setup now draws its log icons over the glass surface without the black strip, in both the summary and Technical details views.

## Developer Access: Neobrutalism Throughout

- Developer Access now uses a complete neobrutalist design: cream backgrounds, solid cards, bold borders, hard shadows, colored metrics, and original SVG artwork.
- The login screen focuses on the sign-in form. The dashboard, ticket cards, create/edit forms, connection settings, and recovery prompts use the same design.
- **Configurable categories:** Enter a category name and use the **+** action when creating or editing a ticket. Saved categories and categories from synchronized tickets are available in ticket forms and dashboard filters.
- Search, status filters, and category filters help locate issues. Narrow forms stack their fields, while shorter windows scroll to keep actions accessible.
- The Easter egg opens after **five completed left clicks on the MCU Flasher toolbar title**, followed by a brief pause. Double-click sequences are counted correctly.
- Local tickets remain available offline. Firebase reachability and sign-in status are checked independently of package Offline Mode.
- Failed connection-setting saves keep the form open and explain the problem. Theme changes preserve drafts, search, and filters.
- Opening failures offer **Retry** and **Show Details**. Missing runtime dependencies or Qt loading failures can offer **Repair and restart**, which saves editor contents and checks active work before handing off to the host's Bootstrap.
- Incorrect Qt imports have been corrected. Application source errors still require a source fix.

The same Developer Access interface is used on Windows and Ubuntu. The main workspace and Resources browser retain their glass themes.

## Boards, Libraries, and Offline Preparation

- **Coverage reports:** Bootstrap audits discovered board declarations and reports prepared targets, ambiguous identities, unavailable definitions, and targets outside the configured plan.
- **Exact board matching:** Generic profiles are not silently replaced with a nearby DevKit or a board with different pin or memory defaults.
- **Separate versions:** Prepared Arduino core versions and vendor indexes retain separate identities.
- **Stronger readiness checks:** Changed declarations, missing tools, damaged packages, or outdated preparation records require preparation again. Failed repair cannot leave an old success record active.
- **Library preparation:** The configured plan and Resources browser provide libraries for supported prepared targets.
- **Safer firmware reuse:** Changes to source bytes, libraries, core inputs, or target settings trigger a rebuild when necessary.
- Hidden discovery entries are excluded from the board picker. Matching also considers declared build flags and USB identifiers.

The default package plan prepares **AVR, ESP32, and ESP8266 with Arduino**, their required toolchains and package variants, and **Servo, ESP32Servo, and ArduinoJson**. Add other platforms, frameworks, or libraries to `direct/offline-packages.json`, then prepare them while online.

**PlatformIO remains the default backend.** Arduino CLI support is restricted to explicitly enabled, exact **Arduino UNO Q** and **Raspberry Pi Pico 2 / RP2350** targets when no compatible PlatformIO target is available. Use **Choose Arduino CLI boards…** in the Resources browser and prepare the selected board support while online. A PlatformIO build failure does not automatically switch compilers.

Bootstrap prepares the Windows Arduino CLI executable. On Ubuntu, it verifies a native executable or provisions the pinned native release. Each operating system keeps its own runtime and package store.

Coverage reporting does not guarantee compatibility with every board. Declared board defaults do not detect a physical device's flash or PSRAM capacity.

## Serial Monitor Improvements

- Long lines wrap to the panel width by default; **Wrap lines** remains configurable. Copying preserves original line breaks.
- UTF-8 is decoded across fragmented reads, and prompts appear without waiting for a newline.
- Undecodable bytes and unsafe control characters appear as visible escapes.
- **Copy** includes retained history and pending output, including data received while display updates are paused.
- **Pause** freezes the display while retaining incoming data within the history budget.
- With **Auto-scroll** enabled, holding the scrollbar pauses following; releasing it returns to the latest output. Disable Auto-scroll to keep reading older output.
- Resizing, theme changes, and timestamp changes preserve selections and reading position.
- **Clear** removes displayed and retained output without resetting the board or replaying a command.

## Build and Upload Improvements

- **Windows deep Cloud builds:** Compile and PlatformIO upload launch through a verified short junction when needed, keeping exact-board caches and build receipts at their canonical locations. This addresses the reproduced ESP32 GCC dependency-file failure on long Windows paths. The Windows workaround is not applied to native Ubuntu.
- Bounded output handling keeps long scans and upload phases responsive.
- Compiler warnings, errors, programmer messages, and final results remain visible while routine dependency chatter is reduced.
- Board, port, and upload-speed controls are locked during the relevant operation phases.
- Connection failures provide clearer boot-button guidance.
- Stop remains available before a critical hardware write begins.
- Automatic ESP connection retries are limited to failures before erase/write starts. A failed hardware write is not automatically replayed.
- Runtime compilation, upload, and reset use prepared local packages. Missing dependencies require Bootstrap preparation.
- Compiler and preparation concurrency adjust to available CPU, memory, and storage resources.

## Editor, Workspace, and Terminal

- Improved offline Monaco syntax scheduling, stale-result handling, and dirty-buffer recovery.
- Cloud Pull reloads the complete source manifest and editor models only after a successful background scan and matching renderer acknowledgement; a failed refresh keeps the editor protected and offers Retry.
- Preserved editor buffers, tab order, keyboard focus, and reading position through layout and theme changes.
- Refined compact layouts, small-window behavior, display scaling, and handling of slower storage.
- Fixed compatibility-list filtering and theme-change issues.
- OpenCode remains embedded in the workspace, with change highlighting and connectivity checks.
- Windows PowerShell/CMD and Ubuntu Bash terminals retain offline xterm assets, Unicode handling, bracketed paste, and bounded output.
- Terminal Clear remains display-only.

## System and Storage Requirements

- **Windows:** Windows 10/11, 64-bit. The supplied launcher uses the application's private Python runtime.
- **Ubuntu:** Native Ubuntu 24.04 or newer on amd64 or ARM64. Raspberry Pi requires a supported 64-bit Ubuntu Desktop image. Ubuntu 22.04 is no longer supported. Launch and setup validate the OS release and CPU architecture before preparing the runtime.
- **CPU:** At least four cores, using physical-core information when available and logical-core fallback otherwise.
- **Ubuntu source setup:** Python 3.10 or newer; use the supported Ubuntu release's native Python. ARM64 uses `/usr/bin/python3` to match Ubuntu's sample-viewer packages. Setup creates a private `.venv-linux` environment.
- **Storage location:** Use a writable SSD or HDD with executable storage for the runtime and native tools. An SSD is recommended for setup and compilation.
- **Connectivity:** Internet access is needed for initial preparation, updates, and additional package installation. Prepared builds use local packages.
- **Serial permissions:** Ubuntu may require `dialout` membership and applicable udev rules.

### Free Space to Plan Before First Setup

| Platform | Estimated minimum free space | Recommended free space |
| --- | ---: | ---: |
| Ubuntu — default AVR / ESP32 / ESP8266 plan | **10 GB** | **20 GB** |
| Windows — default AVR / ESP32 / ESP8266 plan | **15 GB** | **20 GB** |

These are conservative **planning estimates**, derived from the current default plan and runtime/package assets. They replace the older **6 GB** baseline; they are not measured peak installer usage or enforced installer thresholds. GB uses decimal units.

Preparation can temporarily retain downloaded archives alongside extracted packages, the private runtime and installed dependencies. Allow additional room for extraction, repair, build output and package updates. The seed importer copies only missing compatible package groups. Historical package-store and seed sizes are not a current peak-space measurement.

Custom platforms, additional Arduino CLI cores, more frameworks, multiple retained versions, large libraries, or several build caches need extra space. If the application, temporary files, and Ubuntu package store are on separate volumes, ensure each destination has sufficient free space. Keeping both operating systems' prepared resources requires room for both stores.

## Updating and Setup

Keep the application folder together and run setup as your normal desktop account.

**Windows — repair an existing installation while online:**

```text
direct/windows/run.vbs --repair
```

For first launch, open `MCU_Flasher.exe`. The launcher prepares or repairs the private Windows runtime before opening the workspace.

**Ubuntu — prepare packages and launch:**

```bash
python3 direct/ubuntu/setup.py
bash direct/ubuntu/run.sh
```

**Ubuntu — repair an existing installation while online:**

```bash
bash direct/ubuntu/run.sh --repair
```

The portable `MCU_Flasher` launcher works on amd64 and ARM64. Ubuntu setup may request administrator authentication for missing system prerequisites; the application runtime stays in its private environment.

### Ubuntu 26.04 Terminal Preference — October 15 Update

Desktop setup and repair now prefer installed **Ptyxis** on Ubuntu 26.04+, including recovery when
system Python is missing. Ubuntu 24.04 keeps its existing terminal selection;
other installed terminals remain fallbacks. This later launcher update passed
53 isolated launcher/setup/release checks and a real Ptyxis 50.1 command-recorder
probe. The CI results below remain scoped to the earlier Cloud baseline.

### Fresh Dependency Cleanup

- **Fresh** previews owned dependency removals before applying them. On Ubuntu, use `bash cleaner/clean_fresh.sh` to preview and `bash cleaner/clean_fresh.sh --apply` to confirm the reviewed plan.
- Ubuntu cleanup now accepts Python's exact generated root `.gitignore` inside `.venv-linux`. Custom files, unexpected links and other unrecognized content still stop cleanup.
- Cleanup preserves sketches, settings, saved accounts/credentials, protected build caches and AI recovery journals. It resets dependencies rather than erasing the desktop user's identity. Shared native package-store removal remains a separate opt-in.
- The next launch runs Bootstrap to restore missing dependencies; prepare online before relying on offline builds. Windows resources remain separate and are preserved by native Ubuntu cleanup.

See the [maintenance guide](cleaner/README.md) for ownership checks and confirmation requirements.

## Verification and Current Limits

The following results are scoped to source revision [`7a58479`](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/commit/7a584794f9c9028e921b57c719c9645e8eb9748d), checked on October 15:

- **Local native Ubuntu:** Isolated Cloud service/configuration/UI, editor-manifest, exact-board cache, runtime/Monaco Pull and native PTY checks passed. Cloud layouts and the relevant responsive cases passed at **100%, 125%, 150%, 175% and 200% scaling**. The native maintenance verifier passed all **48 checks** before Fresh cleanup.
- **Cloud compile fixture:** The original/restored sources and generated inputs matched for `espressif32:esp32dev`, Arduino and NimBLE-Arduino **2.5.1**. Native host GCC successfully wrote a dependency file through a **420-character path**. The fixture's ESP32 compiler was mocked; this is not certification of a real native NimBLE build.
- **Native first-run CI:** Ubuntu **24.04 and 26.04** jobs passed full isolated default preparation, actual workspace launch and a real baseline ESP32 Compile-button build. SVG/UI, Windows Developer Access and Qt binding diagnostic jobs also passed in the [Desktop compatibility run](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/38670491891).
- **Native layout CI:** All four jobs passed on Windows with Python **3.11 / Qt 6.7–6.11** and **3.14 / Qt 6.12+**, Ubuntu **24.04 / Python 3.12**, and Ubuntu **26.04 / Python 3.14**, within the configured Qt 6.x ranges. See the separate [Native UI layouts run](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/38670491005).

**The broader Desktop compatibility workflow still failed.** Its runtime jobs failed on Windows and Ubuntu; the downstream Cloud, storage, editor, responsive and compiler integration steps in those jobs were skipped. Passing isolated jobs do not establish a complete release certification.

Local Cloud checks used mocked Firebase, credential vaults, persistence and hardware under `temp/`. No live account login, toolchain installation, physical flash or live-cache cleanup was used for those checks. CI first-run installation was confined to disposable fixtures. Real Firebase/keyring integration, native Wayland, physical USB uploads, a full Cloud NimBLE firmware compile and other board/toolchain combinations need separate coverage.

---

**Full Changelog:** [Commits on main](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/commits/main)
