# Rust development tools installed

19 October 2026. Requested conversation:
[Rust Plugins Comparison](https://chatgpt.com/share/6ad601e1-f514-83ec-86dc-e88f93d6ea22).
This records the local Codex setup, separate from application migration and release
qualification. [Exact sources, hashes and checks](tooling-setup.json) are retained.

| Component | Installed result |
| --- | --- |
| Rust Skills | 16 relevant static skills from `actionbook/rust-skills`, pinned at `5c40d3a`; complete helper directories retained |
| Rust Testing | ECC skill pinned at `76aaa77`, with its MIT license |
| Rust LSP skill/server | Pinned at `21a1f9b`; built from its lockfile; six navigation/inspection tools enabled |
| Rust Analyzer and rust-src | Installed for the existing private Rust 1.99.1 toolchain |
| Superpowers | Version 6.4.2 installed and enabled from the curated marketplace; 15 skills, no hooks |
| Context7 | Official Upstash Codex plugin 1.0.2 installed and enabled from source `ae4871e`; one skill and its hosted documentation MCP |

There are **18 separately installed Rust skills**, plus the plugins' bundled
skills. Existing project instructions and pre-existing Codex configuration values
are preserved. No upstream project AGENTS.md, submodule, automatic hooks, browser
infrastructure, blanket coverage gate or compiler-edition change was imported.
The unrelated domains, automatic router and coding table that incorrectly calls
standard-library synchronization primitives deprecated were omitted.

The Rust LSP smoke initialized MCP, enumerated all 13 upstream tools, loaded an
owned dependency-free fixture, obtained analyzer status and found its declared
workspace symbol. The server exited successfully and its owned descendants
retired. Only `hover`, `definitions`, `references`, `workspace_symbols`,
`analyzer_status` and `view_syntax_tree` are exposed in Codex. Startup has a
20-second client deadline; tool calls have a 45-second deadline.

Context7 initialized successfully, exposed both documentation tools, resolved
Tauri and returned documentation. Those checks used anonymous access and public
documentation queries; no application source or account credential was sent.
The plugins' installed/enabled states were checked through Codex's plugin registry.
New registrations need a subsequent Codex turn/session to enter the active skill
and tool list; this setup does not claim they were already loaded into this turn.

Rust LSP remains an experimental development integration. Its read-only semantic
queries can execute trusted Cargo build scripts/procedural macros; it has no
server-side workspace fence and has unbounded internal analyzer requests. The
client deadlines and tool allowlist do not make it a filesystem sandbox. A
Windows executable lookup defect remains upstream. This setup was verified on
Ubuntu AMD64 and does not qualify the application on Windows or physical boards.
Actionbook claims MIT in its README but lacks license text at the pinned revision;
the LSP repository also lacks a license file. Their code/skills are not bundled
into the application's release artifacts.

Cargo emitted two installation advisories: environment-selected pinned toolchain
and binary PATH. The MCP configuration supplies the pinned Rust environment and
uses the server's absolute executable path. No shell profile was changed.
The original configuration was backed up privately before installation; an empty
existing Node runner argument field normalized away by the CLI was restored.

The pushed audit checkpoint is `3c1cdc2`; fully verified application/prototype code
remains `4cf730b`. The native file-identity adapter, controlled probe, Python
verifier and CI wiring remain uncommitted from the interrupted migration stage.
Ten adapter unit checks and Windows target compilation passed; its final
reserved-name refinement and full integrated/native verification remain pending.
Resume that code stage using [HANDOFF.md](HANDOFF.md), without treating these
tool-install checks as application parity or enabling live source writes.

The pending migration note above is historical to the tool-install checkpoint.
The continuation completed native metadata identity, pure Send and matching xterm
notices; consult STATUS.md/HANDOFF.md for current code and exact qualification.
The installed tools were reused without rerunning their installers.
