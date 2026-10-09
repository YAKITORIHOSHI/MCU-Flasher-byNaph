# Maintenance utilities

Windows and Ubuntu maintenance use separate implementations and resources.
Every entry point previews its plan by default. Close MCU Flasher, Bootstrap,
coding terminals and package workers before applying a cleanup. These utilities
refuse active operations; cleanup never stops them automatically.

| Task | Windows | Ubuntu |
| --- | --- | --- |
| Source bytecode | `clean_pycache.bat` | `bash cleaner/clean_pycache.sh` |
| Fresh dependencies | `clean_fresh.bat` | `bash cleaner/clean_fresh.sh` |
| Runtime reset | `DANGER-ZONE/runReset.cmd` | `bash DANGER-ZONE/reset_ubuntu.sh` |

Windows fresh cleanup removes the recognized local `env` / `src/env`, local
PlatformIO stores and owned Windows offline extras, retaining `src/_python`.
Windows runtime reset also removes the recognized private Python runtime.
Aliases are unlinked only when they point into this checkout's Windows store
or modules; another checkout's aliases and their targets remain intact.

Ubuntu fresh cleanup removes the validated `.venv-linux` and source bytecode.
Ubuntu runtime reset also removes receipt-authenticated native tools and Linux
offline extras. Its XDG PlatformIO store is shared by this account's checkouts
for the current architecture. It stays intact unless runtime reset explicitly
selects `--include-native-store`, which also requires a matching native receipt.
Ubuntu maintenance runs with system Python, outside the environment it removes.

Both hosts preserve user sketches, settings, generic logs, recovery copies,
`.mcu_flasher_build_cache`, `.mcu_ai_edits`, `.pio`, `.vscode`, `.clangd`,
`.opencode`, and the other host's resources. Bytecode cleanup prunes protected
and runtime directories before traversal. Unknown content, linked deletion
roots and escaping paths are retained; a refusal or incomplete cleanup returns
a failing exit status. Interior runtime links are never followed into targets.
After dependency removal, relaunch the matching native launcher while online.

From the project root, review a Windows plan with:

```powershell
& cleaner/clean_fresh.bat -NoPause
& DANGER-ZONE/runReset.cmd -NoPause
```

Add `-Apply` when you intend to carry out the displayed plan. Windows cleaning
requires `CLEAN MCU WINDOWS`; direct runtime maintenance requires
`RESET MCU WINDOWS`; the danger launcher requires `RESET MCU PROJECT`.
The cleaner's `-Force` skips its typed acknowledgement for intentional automation
but still requires `-Apply`. The danger launcher always asks for confirmation.

Review Ubuntu plans with:

```bash
bash cleaner/clean_fresh.sh
bash DANGER-ZONE/reset_ubuntu.sh --include-native-store
```

Add `--apply` to carry out the selected plan. Type `CLEAN MCU UBUNTU` for cleaning
or `RESET MCU UBUNTU` for runtime reset. `--yes` skips that acknowledgement for
intentional automation and requires `--apply`.

The legacy `terminate_all_python.exe-task.bat` filename now previews only this
checkout's captured processes. Its explicit `-Apply` requires
`STOP MCU FLASHER`: stopping during an erase/write can leave firmware incomplete.
Unrelated Python programs are never selected by name alone.

`remove_mcu_drivers.bat` previews exact supported Windows driver packages.
`-Apply` requires `REMOVE MCU DRIVERS` before elevation. Removal affects other
applications using the same drivers; in-use removal additionally requires
`-ForceInUse`. Ubuntu USB permissions and system packages are separate.

`DANGER-ZONE/runReset.cmd -FullReset` previews additional shared Windows
application, driver and package-store removals. `-FullReset -Apply` requires
`DELETE MCU INSTALL` and administrator access; shared programs must be closed.
`RUN_Crash_Diagnostic.cmd` previews its Windows system diagnostics and repairs.
An explicit `-Apply` requires `REPAIR WINDOWS`; reports go to
`temp/audit/crash-diagnostic`. Neither entry point elevates or changes Windows
merely because it was opened.

Verification uses `direct/verify_windows_maintenance.py` and
`direct/verify_ubuntu_maintenance.py`, with disposable fixtures under `temp/`.
Never verify these utilities by resetting a live installation or user project.
