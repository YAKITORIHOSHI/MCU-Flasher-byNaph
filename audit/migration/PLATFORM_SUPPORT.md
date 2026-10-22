# Required platform coverage for the Rust replacement

User requirement: 21 October 2026, Asia/Manila. This expands the earlier
Windows AMD64 / Ubuntu-only migration scope. Every platform family below is a
replacement release requirement; current preview evidence does not qualify
the full application on any of them.

## Required matrix and present evidence

AMD means 64-bit x86 (`x86_64` / AMD64), including compatible Intel processors.
ARM means 64-bit ARM (`aarch64` / ARM64). No 32-bit platform is implied.

| Required platform | Native architectures | Current Rust evidence and remaining platform work |
| --- | --- | --- |
| Windows 10 | AMD64, ARM64 | Hosted Windows Server 2022 AMD64 preview/backend checks pass; Windows 10 desktop qualification is still open. A Windows 11 ARM64 portable-core CI job is configured, with native execution still pending. It cannot qualify Windows 10; the AVR preparer currently refuses ARM64. |
| Windows 11 | AMD64, ARM64 | The same hosted AMD64 evidence applies only to its tested host. A native ARM64 contracts/core job is configured separately from host services; its result is pending. Native desktop, runtime/tools/drivers and installation remain open. |
| Ubuntu 24.04 LTS | AMD64, ARM64 | Native hosted preview/backend checks pass on both architectures. Complete services, desktop input/display, packaging and USB/device qualification remain open. |
| Ubuntu 26.04 LTS | AMD64, ARM64 | Local 26.04.1 AMD64 preview/backend evidence exists. Native backend CI for both architectures is configured; its first result is pending. Complete desktop/device qualification remains open; Python 26.04 CI is separate evidence. |
| macOS | Intel x86_64, Apple Silicon ARM64 | Native contracts/core CI jobs for both architectures are configured, with execution still pending. No native GUI adapter, native ownership/process implementation, tool-runtime qualification or packaged app is recorded. |
| Fedora Linux | x86_64, aarch64 | No Fedora-native Rust execution is recorded. Qualify Fedora's own GTK/WebKit packages, setup, desktop/session, SELinux and USB permissions. |
| Linux Mint | AMD64 | No Mint-native Rust execution is recorded. Qualify official Ubuntu-based Mint editions independently, including setup, desktop and USB permissions. No official Mint ARM64 target is established. |

For Linux outside the explicitly requested Ubuntu LTS releases, start with
Fedora Workstation 44 and Linux Mint 22.x (22.3 Cinnamon first) as qualification
baselines. Additional release versions require their own recorded dependency
and runtime checks.
LMDE has a separate Debian base and needs its own qualification before a Mint
support claim includes it. macOS minimum versions must be pinned from the full
UI, WebKit, runtime and firmware-tool closure before packaging; Rust's minimum
deployment target alone does not set this application's supported version.
These version gaps remain open requirements, not silent exclusions or passes.

Exact existing receipts remain authoritative: native UI `49ab312` / runtime
`eb9d13c` in [session evidence](native-project-sessions.json), backend `29ec2b0`
in [package metadata evidence](rust-avr-package-metadata.json), and the local
Ubuntu 26.04.1 records linked by [CURRENT.md](CURRENT.md). No new platform was
built, launched or tested by this requirements amendment.

## Native coverage continuation

The workflow now offers a focused `platform_contracts_only=true` dispatch on
`migration-prototype.yml`. With the other inputs left false, it runs exactly
five native jobs:

- Ubuntu 26.04 AMD64 and ARM64 execute the existing complete backend units,
  strict lint, bounded compile-process ownership fixture and owned compatibility/
  fake-tool controllers. They use the same pinned Rust toolchain and backend
  checks as the existing Ubuntu 24.04 jobs. These crates need no GTK/WebKit or
  GUI development packages; this dispatch deliberately does not run a desktop.
- Windows 11 ARM64, macOS 15 Intel and macOS 15 Apple Silicon execute the real
  `mcu-contracts` and `mcu-core` suites and strict lint. Each selects an explicit
  native Rust host triple and rejects a mismatched compiler host, runner CPU or
  operating system. Python 3.14 is used only for the read-only host receipt;
  the selected official distribution has native binaries on all three hosts.
  No Python application service or firmware tool is launched.

`native_with_platform_contracts=true` combines those five jobs with the three
existing Ubuntu 24.04 AMD64/ARM64 and Windows Server native GUI jobs. Each GUI
job now runs a separate owned Save/Save All check, in addition to retained
workspace and detach checks. This eight-job dispatch skips separate Python
editor/browser jobs whose source is unchanged by the native Save slice.

`backend_only=true` without the focused selector retains the existing Ubuntu
24.04 pair and Windows Server AMD64 jobs, adds the Ubuntu 26.04 pair and runs
the three scoped core jobs. `windows_only=true` selects Windows Server AMD64
backend fixtures and Windows 11 ARM64 core contracts. Browser-only dispatches
remain independent. Each job preserves the actual OS release, runner CPU,
compiler host, source SHA and scoped test logs as artifacts even when a later
step fails. Tests, rather than a capability probe or unsupported fallback,
determine success. This configuration alone is not a recorded native pass.

The three portable-core jobs intentionally do not build or execute `mcu-host`.
Darwin's native file/process ownership adapters are unimplemented, and the AVR
archive preparer explicitly admits neither Darwin nor Windows ARM64. Core
execution establishes portable behavior only; it cannot qualify those missing
adapters, GUI/PTY integration, private tools, launchers, packaging or devices.
The official hosted-runner list supplies the selected native architectures;
macOS's hosted ARM machine also remains below the full app's four-core policy.
See the [GitHub hosted-runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
and [official Python distribution manifest](https://github.com/actions/python-versions/blob/main/versions-manifest.json).

Fedora and Mint still require actual distro-native backend/desktop execution,
their own release/dependency adapters and all release gates below. An Ubuntu
job or a pure core job does not establish either distribution's support.

## Architecture and implementation rules

- Retain native Rust application widgets and services. Keep Monaco/xterm as
  embedded components and Python only in approved private firmware tools.
- Continue Win32 on Windows and GTK on Linux while those adapters qualify.
  Add an AppKit adapter on macOS with system WKWebView for the embedded islands;
  share services and typed contracts rather than treating the Tauri prototype
  as the macOS replacement. The exact AppKit binding needs a reviewed dependency
  choice before implementation.
- Select OS/release, native CPU architecture, process architecture and each
  tool's executable architecture independently. Do not infer the host from a
  board architecture, a filename or an existing package directory.
- Native ARM64 is a separate build/run requirement. Windows emulation or macOS
  Rosetta may be an explicit, separately qualified tool mode; it cannot stand
  in for native GUI/backend or native USB-driver evidence. Windows 10 ARM cannot
  rely on Windows 11's x64 emulation support.
- Preserve early native OS validation and fail before setup, repair or cleanup
  for unknown/missing/malformed release data. Replace Ubuntu-specific admission
  with reviewed per-distribution policies when implemented; accepting every
  `ID_LIKE=ubuntu` value or deleting the guard does not establish Mint support.
- Keep package names, package-manager commands, native keychains/credential
  stores, PTY/process lifetime, USB access and runtime repair behind explicit
  host adapters. Do not route Fedora through Ubuntu's `apt-get` setup.
- Treat GUI availability and exact board/tool availability separately. Admit
  only verified host-compatible compiler/uploader/runtime payloads; show the
  exact unavailable target/tool when a vendor does not supply one. No substitute
  board, unverified host binary or automatic hardware-command replay is allowed.
- Build Linux artifacts against the selected oldest ABI baseline and verify
  them on every required distro/release/architecture. A binary built on local
  Ubuntu 26.04 does not prove Ubuntu 24.04 compatibility. Verify X11 and Wayland
  where supplied; a container-only backend run does not qualify a desktop.

## Concrete implementation gaps

| Area | Current paths | Required next work |
| --- | --- | --- |
| macOS native UI | `desktop/native-ui/Cargo.toml`, `desktop/native-ui/src/main.rs` | Add target-specific native controls, window/menu/chooser ownership, embedded WebViews, Cmd shortcuts, main-thread dispatch and detach/recovery. The current executable exits 78 on macOS. |
| macOS backend authority | `desktop/crates/host/src/file_identity.rs`, `process_identity.rs`, `compile_process_fixture.rs` | Implement native retained file/directory and process identities, coexistence/transaction behavior, cancellation and descendant cleanup. Existing Linux `/proc`, `O_PATH` and subreaper behavior cannot be copied as Darwin assumptions. Preserve unsupported results until these paths qualify. |
| Tool host coverage | `desktop/crates/host/src/prepare_avr_fixture.rs`, `desktop/firmware-tools/` | Extend reviewed host manifests and the complete private-runtime/loader/tool closure for Windows ARM64 and both macOS architectures. The current AVR host enum admits Linux x86_64/aarch64 and Windows x86_64 only. Do not relax its checks to manufacture support. |
| Windows ARM runtime/drivers | `src/modules/private_python_guard.py`, `src/modules/bootstrap.py` | Existing repair selects an AMD64 Python installer; CP210x driver setup equates a 64-bit host with x64. Add architecture-specific admission and reviewed native ARM64 driver/runtime payloads. Existing ARM64 Node/Arduino CLI choices do not qualify the full closure. Native WebView2 loader/runtime must match the app architecture. |
| Linux admission/setup | `src/modules/ubuntu_release.py`, `direct/ubuntu/release_guard.sh`, `direct/ubuntu/preflight.py`, `direct/ubuntu/system_setup.py` | Existing production paths deliberately admit only Ubuntu and use Ubuntu package names/apt. Add Rust release/dependency policies and isolated adapter checks for Fedora and Mint before connecting their setup/repair UI. Preserve the working Python release until qualified replacement. |
| Native CI and evidence | `.github/workflows/migration-prototype.yml`, platform verifier scripts | Execute the configured Ubuntu 26.04 backend pair and the scoped Windows ARM64/macOS Intel/ARM64 core jobs, then retain exact source/results. Complete native host services/GUI on those hosts and add Fedora x86_64/aarch64 and Mint AMD64 execution. Distinguish cross-build, emulated execution, native core, backend, desktop and devices. Hosted Windows Server AMD64 remains separate from Windows 10/11. |
| Distribution | Native launchers/installers and release manifests | Produce host-specific artifacts with matching tool runtimes and notices; verify clean-machine install, update, repair and rollback. macOS needs an app bundle and release signing/notarization; Fedora and Mint need their own dependency/desktop integration. No installer or published artifact is changed by this document. |

## Required release gates for each matrix row

- [ ] Pin the exact OS edition/version, native CPU and application/tool binary
  architectures, dependency versions and tested source/artifact digests.
- [ ] Build and execute native backend and GUI contracts on that host. Record
  absent runners as missing evidence; do not count an empty/unsupported smoke
  fixture or a cross-compilation as a native run.
- [ ] Prove Open/Edit/Save and independent-project behavior, exact target/settings,
  retained dirty buffers, themes, native clipboard/IME/accessibility, resizing,
  mixed DPI/Retina and editor detach/reattach on its actual desktop.
- [ ] Prove a real Rust-controlled exact-target compile with the admitted private
  tool runtime and then the supported board/tool inventory for that host.
- [ ] Qualify native PTY/Unicode/backpressure/cancellation, serial reconnect,
  USB permission/driver behavior and representative real upload/reset workflows.
  Hardware-free checks remain separate and never issue these device writes.
- [ ] Qualify native credentials/cloud/settings and interrupted-write recovery,
  clean standard-user installation, update/repair/rollback, resource budgets
  and offline behavior. Run actual distribution signing/notarization only when
  the release artifact and credentials are ready and authorized.
- [ ] Pass parity and independent review before replacing the production launcher.
  Report per-platform/tool limits explicitly in release notes.

## Primary references checked for this amendment

- [Wry 0.57 platform considerations](https://docs.rs/wry/0.57.0/wry/): native
  WebKit on macOS, WebKitGTK on Linux, Fedora's GTK/WebKit development packages
  and GTK-container embedding for Wayland.
- [Rust macOS targets](https://doc.rust-lang.org/rustc/platform-support/apple-darwin.html):
  distinct Intel and Apple Silicon targets; these compiler targets alone do
  not establish application compatibility.
- [Apple distribution packaging](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution)
  and [notarization](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution).
- [Windows on Arm emulation](https://learn.microsoft.com/en-us/windows/arm/apps-on-arm-x86-emulation):
  Windows 10 ARM x86 emulation and Windows 11 ARM x86/x64 emulation have different
  tool compatibility. Device drivers still require the proper native architecture.
- [Windows on Arm FAQ](https://learn.microsoft.com/en-us/windows/arm/faq) and
  [WebView2 distribution](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution):
  qualify native ARM64 drivers and application-matched WebView2 components.
- [Ubuntu release cycle](https://ubuntu.com/about/release-cycle),
  [Fedora Workstation downloads](https://fedoraproject.org/workstation/download/),
  [Fedora WebKitGTK 4.1 package](https://packages.fedoraproject.org/pkgs/webkitgtk/webkit2gtk4.1-devel/),
  [Linux Mint supported releases](https://linuxmint.com/download_all.php) and
  [Mint 22.3 AMD64 ISO record](https://community.linuxmint.com/iso/view/769).
- [GitHub hosted runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners):
  a separate Windows ARM runner can provide ARM64 execution; it does not replace
  Windows 10/11 physical desktop and USB qualification.
