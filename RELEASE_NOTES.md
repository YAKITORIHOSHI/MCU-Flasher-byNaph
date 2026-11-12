# MCU Flasher by Naph — Release Notes

**Version:** v11 (Stable) · **Released:** October 23, 2026 · **Updated:** November 12, 2026

**Platforms:** Windows 10/11 (AMD64) · Ubuntu 24.04+ (amd64, ARM64) ·
Linux Mint 22.x (amd64) · Fedora 44+ (x86_64, aarch64) · macOS 14+ (Intel, Apple Silicon)

Installed copies update themselves from GitHub on their next launch; see
[Updates](README.md#updates). The full history of changes is in the
[commit log](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/commits/main).

---

## November 12 — Faster updates and small-screen toolbars

- **Updates show their progress.** Downloading an update shows a progress bar,
  the transfer rate and the elapsed time, so a slow connection no longer looks
  frozen. On Ubuntu, a launch from All Apps shows a progress window whose
  **Cancel** opens the installed version instead.
- **Updates download only what changed.** The first update downloads about
  16 MB; later updates fetch only the changed files. Across three recent
  updates that was 0.7 MB instead of 15 MB.
- **Faster start on Ubuntu.** The runtime check now runs while GitHub is being
  checked for updates, instead of after it.
- **Toolbars on small screens.** At half-screen widths the title, build actions
  and project actions keep a clear, even space between them. **Actions** now
  has its own icon. In the two-row Controls layout, the serial port field
  extends to the upload speed, which lines up under the options button.

## November 12 — Responsiveness and AI review

- AI edits no longer freeze the window. The AI Changes tab refreshes once per
  burst, only while visible.
- Notifications and the AI review journal are written in the background, and
  notification history is stored per sketch again.
- A pending AI review that no longer matches its file after a restart is
  dismissed with a notification; the file is left untouched.
- Settings shows what the Automatic interface size selects on the current
  display, and a size change offers **Restart now**.

## November 11 — Uploads, editor and installation

- Uploads write the complete ESP32 image set (bootloader, partition table and
  application) and restart the board on the open Serial Monitor connection, so
  the boot log appears in full.
- The Serial Monitor follows the sketch's `Serial.begin()` rate.
- Ctrl+Click / F12 opens library headers and declarations before the first
  compile.
- Terminal tabs and editor notices have visible close buttons in every theme;
  menus and dropdowns use the workspace's glass style and short animations.
- Installed copies receive updates again: a mis-recorded release revision had
  stopped them.
- Ubuntu installs the application for the current user and adds one All Apps
  entry once setup completes.

## Earlier v11 updates (October 23 – November 10)

- **Serial:** passive monitoring that never resets the board on open or close;
  bounded, responsive send and receive.
- **Workspace:** glass themes, responsive layouts from 800×600 to 8K, a
  detachable editor, compact Actions and Options menus, and an assistant
  loading screen.
- **Builds:** reuse of unchanged builds across actions and boards, faster
  Windows setup, quieter build logs and clearer compiler hints.
- **Projects:** Ghost Sketch, file management from source tabs, project-wide
  search and Cloud sketches with conflict-safe Push and Pull.
- **Platforms:** native launchers for Ubuntu (including ARM64 and Raspberry
  Pi), Linux Mint, Fedora and macOS, with a PySide6 board and library browser.

## Requirements

- At least 4 CPU cores.
- Free space for the default AVR / ESP32 / ESP8266 setup: about 10 GB on Linux
  or 15 GB on Windows; 20 GB recommended.
- Internet access for the first setup.

## Known limitations

- Windows on ARM is not supported.
- USB upload on Linux Mint, Fedora and macOS has not yet been verified on
  physical hardware.
- macOS builds are not notarized; the first launch needs Control-click → Open.
- Keyboard routing on native Windows 11 is verified only on hosted Windows CI
  so far.
- An application update refreshes the source only. It does not replace the
  bundled Windows runtime; download a new release archive for that.
