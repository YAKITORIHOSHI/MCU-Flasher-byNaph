# MCU Flasher by Naph — Release Notes

**Version:** v11 (Stable) · **Released:** October 23, 2026 · **Updated:** November 14, 2026

**Platforms:** Windows 10/11 (AMD64) · Ubuntu 24.04+ (amd64, ARM64) ·
Linux Mint 22.x (amd64) · Fedora 44+ (x86_64, aarch64) · macOS 14+ (Intel, Apple Silicon)

Installed copies update themselves from GitHub on their next launch; see
[Updates](README.md#updates). The full history of changes is in the
[commit log](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/commits/main).

---

## November 14 — Lite multiple sketches and shared serial ports

- Lite opens several sketches at once, each in its own window, like the main app.
- The main app and Lite never select the same serial port: a port chosen in one
  is shown as in use in the other and refused, including at Connect and Upload.
- Lite's Open sketch, Resources and board picker stay in front until closed, and
  the large empty space in Open sketch is fixed.
- Reopening Lite right after closing it no longer reports that another workspace
  is using its state.

## November 14 — Lite screen fit, visible Developer Access and reliable Stop

- Fix Windows staying closed after an automatic update said it was restarting:
  the restarted Bootstrap now takes over the startup lock instead of reporting
  that MCU Flasher is already starting.
- Fix Windows Developer Access reporting Ready with no visible window: preserve
  native GUI startup and verify the window before acknowledging readiness.
- Keep the main workspace usable during Developer Access authentication, ticket
  forms and notices. Reopening retains its maximized state; closing the workspace
  retires its portal safely.
- Keep Lite's Stop button visible. Enable it only for cancellable work and explain
  protected writes, cleanup, idle and stopping states.
- Fit Lite to scaled, short and portrait desktops. Keep complete action rows,
  provide Monitor/Sketch & Connection views in narrow windows, reserve scrollbar
  space outside cards, and keep dialogs and monitor rims within their panes.
- Cancel library searches through an owned worker with a 30-second elapsed budget;
  stopped work cannot publish late results or success. Retire only that operation's
  child processes on Windows and Ubuntu.
- Bootstrap's disclosure alternates **Technical details** and **Activity summary**.
  Expand isolated Windows/Ubuntu CI to exercise the actual Rust renderer as well
  as layout, portal, cancellation and backend regressions.

## November 13 — Lite Windows interaction repair

- Align the board picker with the main app: stable left-aligned rows, deduplicated
  recent targets, explicit confirmation, keyboard navigation and bounded long names.
- Preserve five logo clicks batched in one frame and avoid counting layout retries.
  Add Developer Access startup acknowledgement, an owned Windows focus handoff,
  startup diagnostics and clean standalone helper shutdown.
- Native Windows Server 2025 CI passes on Python 3.11/3.14: widget/input checks,
  the real portal at 100–200 percent, and worker/history/preparation checks. Keep Lite's existing colorway,
  authentication, automatic preparation and exact-board firmware reuse.

## November 13 — Isolated Lite workbench and complete Bootstrap sources

- Added `LITE-MODE`: a separate Rust workbench with Python build/upload tools,
  recent sketches/boards, search, stable monitors and automatic certified build reuse.
- Added active Light/Dark labels, colored Connection controls, original-folder
  filename links and five-click access to the existing authenticated developer portal.
- Bootstrap now carries Lite and future public application files automatically
  from tracked checkout files or the release's generated source manifest, while
  excluding private state, runtimes and development scratch.
- Lite supports Ubuntu and Windows. Windows release executables are cross-built;
  native Server widget/portal/worker checks pass. Windows 11, native Rust rendering
  and physical USB qualification remain manual gates.

## November 13 — Assistant loading and Developer Access tickets

- **Quieter Assistant startup.** Removed the elapsed-time counter from the
  loading screen. Help for a slow start and startup output remain available.
- **No ghost ticket when creating.** New ticket opens one dedicated form;
  a card appears only after **Create ticket** succeeds. Cancelling adds nothing.
- **Ticket deletions reach other devices.** A stale cache no longer brings
  deleted tickets back, including after deleting the last ticket. A failed
  cloud deletion restores its card and reports the failure. Tickets created
  while disconnected remain available to sync when the connection returns.

## November 12 (later) — Bootstrap window on Ubuntu, updates without questions

- **Ubuntu has the Bootstrap window.** Updates, repairs and opening the app now
  show the same Bootstrap window as Windows instead of a terminal or a small
  progress box. Only the very first setup, before the app's runtime exists,
  still uses a terminal.
- **Updates no longer ask.** Updates are on by default and install without a
  question on both systems. **Skip Updates** on Windows still turns them off.
- **Much smaller updates.** An update reuses the files already installed and
  never downloads the Windows driver installers or board catalogs it does not
  replace. A typical update now takes seconds instead of minutes.
- **No double downloads.** Opening MCU Flasher twice while an update is
  downloading no longer downloads it twice; the second launch waits for the
  first.
- **A copied folder hands over to the installed app.** Launching an older
  copy whose runtime needs repair now opens the installed app directly instead
  of starting setup in a terminal.

## November 12 — Faster updates and small-screen toolbars

- **Updates show their progress.** Downloading an update shows a progress bar,
  the transfer rate and the elapsed time, so a slow connection no longer looks
  frozen. On Ubuntu, a launch from All Apps shows a progress window whose
  **Cancel** opens the installed version instead.
- **Updates download only what changed.** The first update downloads about
  16 MB; later updates fetch only the changed files. Updating from the previous
  version to this one took 13 files (0.4 MB, under 2 seconds) instead of 16 MB.
- **A dropped connection no longer restarts the download.** The update tries
  once more, then waits for the next launch and keeps what it already has.
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
