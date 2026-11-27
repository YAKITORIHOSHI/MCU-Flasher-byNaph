# MCU Flasher Lite 0.1.0 — Release Notes

**Platforms:** Ubuntu 24.04+ (x64; ARM64 with a native build) · Windows 10/11 (x64)

MCU Flasher Lite is a lightweight native workbench for compiling, uploading and
monitoring sketches you edit in your own editor. It ships inside MCU Flasher and
uses the same build tools, boards and libraries. To open and use it, see the
[Lite guide](README.md).

## What it does

- **Sketches.** Offers a system folder chooser and recent sketches on startup, with
  the last sketch ready to reopen. Lite validates a folder or source path before opening the sketch. Each sketch
  has its own workspace window. Lite refreshes the open sketch as you save
  changes in your editor.
- **Boards.** Searches the official PlatformIO board catalog by name, ID, vendor,
  platform, MCU or framework, ranks exact matches first and suggests close
  matches for typos. Each board is listed once, with its platform, MCU and ID.
  Selecting a board sets its platform and default framework
  using the main app's catalog, without a separate framework dropdown.
- **Compile, Upload and Clean.** Runs the main app's build and upload actions.
  Missing tools are prepared automatically before the first build. After tool
  preparation, Lite reads the latest saved source bytes. Upload reuses firmware
  when the saved build for that board still verifies against those bytes.
- **Monitors.** Separate Build and Serial monitors with Follow, Copy and Clear.
  Connecting never resets the board. The Serial monitor holds its baud rate; the
  Connection panel holds the port, upload speed and send controls. The baud menu
  is the Arduino IDE's (300 to 2000000) plus 1500000. ESP boards upload at
  115200 to 2000000, and 2000000 asks first because some adapters cannot keep
  up; other boards, such as the Arduino UNO, keep their own upload speed.
- **Shared tools.** Lite and the main app use one package store, so boards and
  libraries prepared in either are available in both.
- **Resources.** Lists installed boards and libraries with their example
  sketches and source files, including those inside ZIPs, beside a read-only code
  viewer with syntax colors, copy and find. **Find more** searches and installs
  from the PlatformIO registry.
- **Safe ports.** A serial port selected in one Lite or MCU Flasher window is
  shown as in use and cannot be selected in another.
- **Stop.** Always visible; it cancels preparation, searches and other work you
  started that can be stopped safely, and explains when it cannot.
- **Remembered state.** Reopens in the theme, window size, monitor view, speeds and
  board it was left with. A port is never chosen automatically.
- **Interface.** Light and Dark themes, layouts that fit small, scaled and
  portrait screens, and a narrow view that switches between Monitor and
  Sketch & Connection. Board results and recent sketches scroll while their
  confirmation buttons stay visible.
- **Developer Access.** Opens the main app's private ticket portal with the same
  account sign-in, aligned filter fields and evenly inset ticket counts.

## What it contains

| Path | Contents |
| --- | --- |
| `UBUNTU`, `WINDOWS.exe` | Ready-to-run Ubuntu x64 and Windows x64 executables |
| `run.sh`, `WINDOWS.cmd` | Build-and-launch fallbacks |
| `src/`, `Cargo.toml`, `Cargo.lock`, `build.rs` | The Rust interface source |
| `python/` | The background worker that runs builds, uploads and searches |
| `assets/` | Icons and interface images |
| `tools/` | Optional runtime setup, native build and verification scripts |

## Requirements

- MCU Flasher's first-launch setup (Bootstrap), which Lite runs automatically
  the first time it is opened. On Windows, the shared release ZIP supplies
  the CP210x and CH34x USB drivers and the WebView2 installer.
- Rust only to rebuild the executables, not to use them.

## Known limitations

- Ubuntu ARM64 needs a native build (`UBUNTU_ARM64`).
- Native Windows 11 and physical USB upload still need verification on real
  hardware.
