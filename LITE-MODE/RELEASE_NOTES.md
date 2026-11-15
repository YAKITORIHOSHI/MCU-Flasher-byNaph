# MCU Flasher Lite 0.1.0 — Release Notes

**Platforms:** Ubuntu 24.04+ (x64; ARM64 with a native build) · Windows 10/11 (x64)

MCU Flasher Lite is a lightweight native workbench for compiling, uploading and
monitoring sketches you edit in your own editor. It ships inside MCU Flasher and
uses the same build tools, boards and libraries. To open and use it, see the
[Lite guide](README.md).

## What it does

- **Sketches.** Opens a sketch folder, or a dropped folder or source file, and
  reads it without modifying it. Each sketch opens in its own Lite window, and
  recent sketches and boards are remembered.
- **Boards.** Searches the official PlatformIO board catalog by name, ID, vendor,
  platform, MCU or framework, ranks exact matches first and suggests close
  matches for typos.
- **Compile, Upload and Clean.** Runs the main app's build and upload actions.
  Missing tools are prepared automatically before the first build, and Upload
  skips compiling when the saved build for that board still matches the sketch.
- **Monitors.** Separate Build and Serial monitors with Follow, Copy and Clear.
  Connecting never resets the board; the Connection panel holds the port, baud
  rate and send controls.
- **Shared tools.** Lite and the main app use one package store, so boards and
  libraries prepared in either are available in both.
- **Resources.** Lists installed boards and libraries with their example
  sketches and source files, including those inside ZIPs, in a read-only code
  viewer with syntax colors, copy and find. **Find more libraries** searches and
  installs from the PlatformIO registry.
- **Safe ports.** A serial port selected in one Lite or MCU Flasher window is
  shown as in use and cannot be selected in another.
- **Stop.** Always visible; it cancels preparation, searches and other work that
  can be stopped safely, and explains when it cannot.
- **Interface.** Light and Dark themes, layouts that fit small, scaled and
  portrait screens, and a narrow view that switches between Monitor and
  Sketch & Connection.
- **Developer Access.** Opens the main app's private ticket portal with the same
  account sign-in.

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
  the first time it is opened.
- Rust only to rebuild the executables, not to use them.

## Known limitations

- Ubuntu ARM64 needs a native build (`UBUNTU_ARM64`).
- Native Windows 11 and physical USB upload still need verification on real
  hardware.
