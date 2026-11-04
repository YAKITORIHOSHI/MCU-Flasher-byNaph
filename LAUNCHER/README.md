# Launch MCU Flasher

Keep this entire `LAUNCHER` folder inside the complete application folder.
Move or copy the whole application together; the entries locate it without
relying on the current working directory.

| Entry | Current release |
| --- | --- |
| `WINDOWS.exe` | Windows 10/11 AMD64 GUI executable; starts the existing Bootstrap without a console. |
| `UBUNTU` | Native AMD64 executable for Ubuntu 24.04 or newer. |
| `UBUNTU_ARM64` | Native ARM64 executable for Ubuntu 24.04 or newer on ARM devices. |
| `LINUX_MINT` | Native AMD64 executable for Linux Mint 22.x, based on Ubuntu 24.04. |
| `FEDORA` / `FEDORA_ARM64` | Native AMD64 / ARM64 executables for Fedora 44 or newer. |
| `MacOS.command` | Script Terminal opens, for macOS 14 or newer on Intel or Apple Silicon. |

See [what is verified on each system](../direct/OTHER_PLATFORMS.md) before relying on
USB upload on Linux Mint, Fedora or macOS.

There are no `.desktop` shortcuts in this folder. Linux entries are real ELF
executables. Choose the one matching your CPU, then double-click it from the
extracted application folder. `MacOS.command` is an executable script recognized
by macOS Terminal. If copying loses Unix executable permissions, restore
**Allow executing file as program** in Properties. A drive mounted with execution
disabled must be moved to an executable filesystem. No system menu shortcut or
fixed absolute path is installed by this folder.

macOS marks downloaded files as quarantined, so the first open needs Control-click,
then **Open**. On macOS 15 or newer, use **System Settings > Privacy & Security >
Open Anyway**. The application is not notarized.

Each entry's name only selects its CPU. Admission reads the real operating system
release, so a Linux entry on an unsupported system refuses before changing anything.
Windows ARM64 remains refused. These entries launch the current application; the native Rust replacement is
still under development. The old root entries remain available for existing
installations.

First launch retains the operating system, architecture, CPU and runtime checks.
Setup may open its normal progress window or request operating system
authentication; only the distribution's own package manager (`apt-get` or `dnf`)
receives administrator rights. macOS setup needs no administrator rights. Unsupported or malformed host information is refused. Preserve
the application folder, user sketches and settings.

Command-line arguments are forwarded literally. For example,
`./LAUNCHER/UBUNTU --check` uses the existing read-only diagnostic mode.
The entries do not select hardware, compile firmware, upload or reset a board.

Rebuild with `python direct/build_launcher_folder.py` on a matching Linux CPU
or Windows host; `MacOS.command` is a script and needs no build. The builder uses an available C / .NET Framework compiler and
installs nothing. Firmware tools and the application are never run by this builder.
