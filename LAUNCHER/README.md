# Launch MCU Flasher

Keep this entire `LAUNCHER` folder inside the complete application folder. Move
or copy the whole application together; these entries resolve their location
each time and do not depend on the current working directory.

| Entry | Current release |
| --- | --- |
| `WINDOWS.vbs` | Double-click on Windows 10/11 AMD64. Starts the existing Windows Bootstrap without a console. |
| `UBUNTU.desktop` | Double-click on native Ubuntu 24.04 or newer, AMD64 or ARM64. Starts the existing Ubuntu Bootstrap and workspace. |
| `UBUNTU` | Executable shell fallback for the same Ubuntu entry; run `bash LAUNCHER/UBUNTU` from the application folder if your file manager opens scripts as text. |
| `MacOS.command` | Availability notice only. macOS Intel/Apple Silicon application support is pending. |
| `FEDORA.desktop` | Availability notice only. Fedora application support is pending. |
| `LINUX_MINT.desktop` | Availability notice only. Linux Mint application support is pending. |

The notices for pending platforms do not install anything or fall back to a
different operating system's application. Windows ARM64 remains pending. These
entries launch the current application; the native Rust replacement is still
under development.

Ubuntu Files may ask you to allow executing the entry as a program or mark it
trusted with **Allow Launching**. These are normal desktop permissions. Launch
from the extracted application folder, not from inside a ZIP viewer. If a copy
loses executable permissions, restore them in Properties, or use the Bash
fallback above. Some file managers show the `.desktop` extension until trusted.
No system menu shortcut or absolute path is installed by this folder.

First launch retains the existing operating system, architecture, CPU and
runtime checks. Setup may open its normal progress window or request the normal
operating system authentication. Unsupported or malformed host information is
still refused. Preserve the application folder, user sketches and settings.

Command-line arguments are forwarded to the existing entry. For example,
`bash LAUNCHER/UBUNTU --check` uses its read-only diagnostic mode. The new entries
do not select hardware, build firmware, upload, or reset a board themselves.
