# Native Rust widget decision

**21 October platform amendment:** the replacement must also cover native
Windows ARM64, Ubuntu 26.04 LTS, macOS Intel/Apple Silicon, Fedora and Mint.
See the [required matrix](../audit/migration/PLATFORM_SUPPORT.md) for the full
OS/architecture scope, existing evidence and unqualified hosts. Retain GTK on
Linux and Win32 on Windows; add native AppKit controls with WKWebView islands
on macOS, subject to binding/dependency and desktop qualification. The original
two-adapter comparison below records the earlier narrower requirement and does
not establish macOS, Fedora, Mint or Windows ARM64 compatibility.

20 October 2026. **Provisional selection: retain GTK3 on Linux and Win32 on
Windows, with Wry hosting only offline Monaco and xterm.** Application commands,
ownership and backend services belong to Rust; Python remains limited to approved
external firmware tools. This selects the implementation direction, not a
production replacement.

The pinned fixture uses `gtk = 0.18.2`, `wry = 0.57.0` and Windows
`windows-sys = 0.61.2`. Its
[recorded evidence](../desktop/native-ui/README.md) includes 17 native smoke
checks on Ubuntu 24.04 AMD64/ARM64 and Windows Server 2022. It does not qualify
Windows 10/11 desktop input or physical hardware.

## Official support comparison

| Candidate | Controls and embedded components | Required hosts and dependency cost | Decision |
| --- | --- | --- | --- |
| GTK3/Win32 + Wry | Real GTK widgets and Win32 controls, with two separate child WebViews. Wry documents GTK-container embedding for X11/Wayland; raw-window child embedding is Linux X11 only. | Ubuntu needs GTK3, WebKitGTK 4.1 and their native dependencies. Windows uses OS controls plus the WebView2 Runtime. Existing AMD64/ARM64 fixtures provide project evidence. Two native adapters must be maintained. | Retain provisionally. [Wry 0.57](https://docs.rs/wry/0.57.0/wry/), [Win32 controls](https://learn.microsoft.com/en-us/windows/win32/controls/window-controls). |
| gtk4-rs | Real GTK4 widgets. Wry 0.57's `build_gtk` accepts GTK3 `Container`, so it is not a GTK4 replacement API. Linux would need a new WebKitGTK 6.0 adapter. | GTK4/WebKitGTK 6.0 packages exist for Ubuntu AMD64/ARM64. Windows GTK4 supports MSVC or GNU but adds GTK runtime distribution and still needs a qualified WebView2 bridge or the separate Win32 adapter. | Credible future Linux migration; no demonstrated integration advantage yet. [Wry API](https://docs.rs/wry/0.57.0/wry/trait.WebViewBuilderExtUnix.html), [WebKit migration](https://webkitgtk.org/reference/webkit2gtk/2.39.90/migrating-to-webkitgtk-6.0.html), [Ubuntu packages](https://packages.ubuntu.com/noble/libwebkitgtk-6.0-4), [Windows setup](https://gtk-rs.org/gtk4-rs/stable/latest/book/installation_windows.html). |
| Slint | Slint renders widget scenes through its renderers rather than using GTK/Win32 controls. Since 1.16, Fluent is the default across platforms. | Windows 10/11 and Linux X11/Wayland are documented. Renderer and optional Qt dependencies add cost; Wry would still add Linux GTK/WebKit dependencies. Winit handles do not establish Wayland child-WebView support. Ubuntu 24.04 ARM64 integration remains unproven here. | Defer: it introduces another rendering stack and new embedding work, with no demonstrated integration advantage here. This is an inference about this architecture; Slint is a native GUI option. [Rendering](https://docs.slint.dev/latest/docs/slint/guide/backends-and-renderers/backends_and_renderers/), [style policy](https://slint.dev/blog/default-native-style-change), [Windows](https://docs.slint.dev/latest/docs/slint/guide/platforms/desktop/windows/general/), [Winit](https://docs.slint.dev/latest/docs/slint/guide/backends-and-renderers/backend_winit/). |

## Distribution decision and production gates

Prefer distribution-managed GTK/WebKit on Ubuntu and shared Evergreen WebView2 on
Windows, with an offline Evergreen installer for first-run provisioning. Final
installer integration remains pending. Fixed WebView2 requires application-owned
security updates and cannot run from UNC/network locations, making it a poor
default for the portable workflow. See [Microsoft distribution guidance](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution).
No application bundle-size estimate is claimed: measure clean-host prerequisite
downloads, installed dependency closure, executable/assets and process-tree RAM
separately on each architecture.

Do not repeat the historical claim that gtk3-rs remains archived: development
resumed and the [maintenance advisory was withdrawn](https://rustsec.org/advisories/RUSTSEC-2024-0414.html).
The pinned 0.18.2 stack still needs dependency/security review and a compatible
update policy; upstream recovery does not qualify this exact lockfile.

Production requires real Ubuntu X11/Wayland AMD64/ARM64 and Windows 10/11
focus, keyboard, IME, clipboard, accessibility, DPI/mixed-monitor and editor-only
detach qualification; full native workspace/backend parity; resource budgets;
complete notices; offline install/repair/update/rollback and device workflows.
No further user toolkit choice blocks reversible implementation. Revisit GTK4
if qualification or dependency servicing blocks the selected Linux adapter.
