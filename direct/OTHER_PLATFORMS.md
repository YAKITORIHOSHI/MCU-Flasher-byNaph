# Linux Mint, Fedora and macOS

These systems run the real application through their own adapters. Ubuntu's guide is
[UBUNTU.md](UBUNTU.md); [the verification record](../audit/PLATFORM_VERIFICATION.md) says
exactly what has been tested on each system. In short, compile and the workspace are
tested in hosted CI, and flashing a real board over USB has not been tested on these
three systems yet.

| System | CPU | Entry in `LAUNCHER/` | Packages installed by |
| --- | --- | --- | --- |
| Linux Mint 22.x | amd64 | `LINUX_MINT` | `apt-get`, using Ubuntu 24.04 names |
| Fedora 44 or newer | x86_64, aarch64 | `FEDORA`, `FEDORA_ARM64` | `dnf` |
| macOS 14 or newer | Intel, Apple Silicon | `MacOS.command` | Nothing system-wide |

Allow about 10 GB of free space, and 20 GB to be comfortable. At least four CPU cores
are required.

## Linux Mint

Mint uses the same setup as Ubuntu, with its Ubuntu 24.04 package names. It is admitted
only as 22.x on that base. LMDE is based on Debian rather than Ubuntu, so it is refused,
and so is Mint 23 until it has its own checks.

```bash
./LAUNCHER/LINUX_MINT            # or double-click it
./LAUNCHER/LINUX_MINT --check    # read-only diagnosis
./LAUNCHER/LINUX_MINT --repair   # rebuild the private runtime
```

To use a USB serial device, add your account to `dialout` and sign in again:
`sudo usermod -aG dialout "$USER"`. USB programmers may also need PlatformIO's udev rules.

## Fedora

Fedora never goes through Ubuntu's `apt-get` setup. Bootstrap reads the libraries and
tools that are missing, then runs `sudo dnf install` (or `pkexec` from a desktop) for only
those packages. To install them yourself, `--check` prints the exact command.

```bash
./LAUNCHER/FEDORA                # FEDORA_ARM64 on aarch64
./LAUNCHER/FEDORA --check
./LAUNCHER/FEDORA --repair
```

- **Atomic desktops** (Silverblue, Kinoite): `dnf` cannot change the system, so Bootstrap
  prints the `rpm-ostree install` command to run instead. Restart and launch again.
- **USB serial:** the same `dialout` step as Mint applies.
- **ARM64:** PyQt5 has no Linux ARM64 wheels, so the sample viewer uses Fedora's
  `python3-qt5` and `python3-qscintilla-qt5`.

## macOS

Open `LAUNCHER/MacOS.command`. Terminal shows first-run progress, then the workspace opens
and keeps running after you close that window.

The first launch downloads a private Python (about 27 MB, verified by SHA-256) into the
application folder, so it needs no administrator rights, Homebrew or Xcode. If you plan
to use libraries from Git, macOS offers to install its command line tools the first time
Git is needed.

The application is not notarized. On the first open, Control-click the file and choose
**Open**. On macOS 15 or newer, use **System Settings > Privacy & Security > Open Anyway**.
You can also clear the quarantine mark on the extracted folder:

```bash
xattr -dr com.apple.quarantine "/path/to/the/application folder"
```

```bash
bash direct/macos/run.sh --check    # read-only diagnosis
bash direct/macos/run.sh --repair   # rebuild the private runtime
```

Known limits on macOS:

- Run it natively, not through **Open using Rosetta**, which is refused.
- A tool that fully detaches from the built-in terminal keeps running after the session
  closes, because macOS has no equivalent of Linux's process subreaper.
- Some vendors publish no Apple Silicon toolchain. PlatformIO then selects its Intel build,
  which needs Rosetta 2: `softwareupdate --install-rosetta`.
- There is no cleaner entry yet. To reset, delete `.venv-macos` and `.macos-tools` from the
  application folder, plus `~/.local/share/mcu-flasher` for the toolchains.
