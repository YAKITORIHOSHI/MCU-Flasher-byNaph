# Rust/Tauri migration prototype

This is the parallel, fixture-only migration workspace. The Python launchers
still start the production application. Full feature parity is required before
replacement; the current preview has no firmware build/upload, real serial,
cloud, downloader, setup or account functionality.

The preview loads the existing offline Monaco and xterm resources, maintains
document models across tabs/themes/layout changes, reads and saves disposable
root sources through typed Rust commands, creates the original project templates
with optional header includes, and streams one fixed child through a native PTY.
It can open only its host-owned sandbox under `audit/migration/scratch/`.
Browser development uses an explicitly labeled memory-only adapter.

Unchanged saves and undo retain original source bytes, including BOM and mixed
newlines. Edited saves preserve BOM and visibly disclose consistent newline
normalization. Fixed-child output advances after an exact renderer-consumed byte
acknowledgement scoped to its native window and stream. Isolated OS-lock proofs
compare Rust with the original Python transaction lock. Pure ownership plans,
native PID creation-time queries and locked registration fixtures now preserve
legacy project/port claims and unknown fields. The registry validates opaque JSON
fragments with `IgnoredAny`, retains their exact value spelling and composes only
the caller's changed ownership fields. It does not convert unknown metadata into
`serde_json::Value`, so large Python integers and private marker-shaped objects
survive claim/release. The internal fragment representation does not require
enabling serde_json's `raw_value` feature in the host dependency.
`float_roundtrip` preserves known creation timestamps.
Tauri transitively enables serde_json's `raw_value` feature; generic Value parsing
can reinterpret its private marker object, so native unknown metadata must remain
opaque even when the host's direct dependency does not enable that feature.
Live ownership remains gated:
native file identity, coordinated activation/release and package-store ownership
are unfinished, and the legacy schema permits one workspace per process.

## Build and run

Use Rust **1.99.1** with rustfmt/Clippy, Node **24**, pnpm **11.25.0**, and Python
3.10+ with `psutil>=5.9,<8` for reference verification. Prepare that dependency in
the selected development/reference environment before running verification;
the scripts do not install it automatically. Activate that environment first:
the Ubuntu script invokes `python3`, and the Windows script invokes `python`.
Python is a development/reference dependency here; the eventual application
retains private Python only for approved firmware tools. Dependencies are locked
in Cargo.lock and pnpm-lock.yaml.

Ubuntu needs a native C/C++ toolchain, GTK3 and WebKitGTK 4.1 development packages
(plus the usual Tauri 2 Linux prerequisites). Windows needs the MSVC C++ build
tools, MSVC Rust toolchain and WebView2. See the official
[Tauri prerequisites](https://v2.tauri.app/start/prerequisites/) and the exact
native CI setup in `.github/workflows/migration-prototype.yml`.

From the repository root on Ubuntu:

```sh
bash desktop/scripts/run-prototype.sh
bash desktop/scripts/verify-local.sh
```

On Windows:

```powershell
powershell -File desktop/scripts/run-prototype.ps1
powershell -File desktop/scripts/verify-local.ps1
```

The verification scripts fail on command failures, Rust lint warnings and Python
warnings. They run the frontend tests/build, Rust tests/lints, controlled PTY,
Python/Rust differential fixtures, native process/registration proofs and native
webview self-check. The process comparison uses only its own controlled child
and psutil's direct creation-time query; it never enumerates the PID inventory.
The registration proof opens disposable `settings.json` files and Python's
original lock implementation, never the live GUI settings module. The native
check writes `audit/migration/native-webview.json` and exits. It checks model
history through public model APIs; it does not qualify native keyboard shortcuts,
IME, clipboard, focus, real dialogs or mixed-monitor interaction.

The optional ignored `.migration-tools/` holds checkout-local development tools
and the local SDK on this development host; it is not shipped or required when
the corresponding system prerequisites are installed. `desktop/dev-env.sh`
selects those tools when present. Probes use the checkout's owned Cargo target
directory, avoiding stale binaries from inherited target-directory overrides.
The latest local full-prototype check used private Xvfb and Mesa from extracted
checkout-local tools, with no system package installation. This renderer setup
is recorded separately from physical-desktop and GPU qualification.

## Boundaries and evidence

- `crates/contracts/`: versioned request/reply identities and structured errors.
- `crates/core/`: fixture project service and pure ports of preferences, serial
  text, resource budgets, target transport and ownership. Its
  [README](crates/core/README.md) records filesystem,
  persistence and compatibility gaps that block live use.
- `crates/host/`: fixed self-child PTY, native process identity, interoperable
  fixture locks and disposable registration transactions. No arbitrary
  shell or hardware command is exposed. The ConPTY fixture cursor reply is for
  this empty terminal only; interactive sessions need their actual renderer.
- `src-tauri/`: fixture host, narrow permissions/CSP, native monitor fitting and
  a gated whole-webview transfer self-test. Editor-only detach is unfinished.
- `ui/`: TypeScript interface and local resource adapters. See
  [asset provenance and release gates](ui/ASSET-NOTICES.md).

The [durable status](../audit/migration/STATUS.md),
[parity ledger](../audit/migration/parity.md) and
[handoff](../audit/migration/HANDOFF.md) distinguish implementation from native
qualification. Passing a hosted runner does not verify Windows 10/11 desktops,
a physical Raspberry Pi, USB uploads or the finished application.

Build output stays in `desktop/target/` and `desktop/ui/dist/`; large logs,
captures and disposable projects stay in the ignored `audit/migration/`
subdirectories. Compact reports and handoffs are tracked. Do not point the
prototype at live sketches, settings, tool stores, journals or cloud records.

## Code checkpoint and remaining gates

At `4cf730b`, local verification passes 90 Rust tests, 40 frontend tests,
46 native webview checks and 23,909 differential cases. Separate native fixtures
pass eight OS-lock, six process-identity and nine competing-registration checks.
The preference comparison contributes 1,335 of the pure cases; the registry
has 21 Rust tests. The process creation time matches psutil exactly on the local
Ubuntu AMD64 host. These counts describe fixtures rather than completed features.

At `4cf730b`, all five migration jobs passed: Ubuntu 24.04 AMD64/ARM64 and
Windows Server 2022 native prototypes each passed 46 webview checks, and both
Windows editor binding ranges passed the 12-test suite at 100% and 200%.
The separate 14-job compatibility, four-job native-layout and native ARM64
workflows also passed at this exact revision. See the audit's `ci-results.json`
for job links and source-qualified results. Hosted runners do not qualify
Windows 10/11 desktops, physical Pi/USB, or the completed replacement.

The latest Qt editor review suite passes 12/12 at 100% and 200% on private Xvfb; the earlier 10-case native-display suite also passed using
native child viewports while retaining models, buffers and focus. Its 200% run
verified Mesa OpenGL llvmpipe, but Vulkan initialization was still observed;
NVIDIA hardware rendering at 200% remains incomplete. This is functional evidence,
not a GPU-free claim or performance benchmark, and does not qualify Tauri input.

Live file-handle identity and replacement races, coordinated legacy/new
activation, package event-root agreement, editor-only detach/input behavior,
full UI/services/tool workflows, licensing and release packaging remain gates.
Windows 10/11 desktop sessions, physical Pi 5 sessions and USB hardware tests
have not been performed for the replacement.
