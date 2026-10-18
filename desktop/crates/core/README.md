# Project service migration boundary

`mcu-core` is a GUI-independent initial project service. It is not a replacement
for the current project controller, AI journals, cloud synchronization, tool
ownership, or editor recovery. The running Tauri preview must use `fixture`.

The native host owns one `ProjectService`, serializes its worker-thread calls,
and supplies the invoking window label as `owner`. Renderer input must never
choose that owner. `register_project` accepts a host-approved absolute root;
subsequent source operations accept scoped workspace/document identities only.
An existing owner/root registration is reused. A different owner cannot register
the same canonical root within this service instance.

```rust,ignore
let mut service = ProjectService::fixture(application_root, explicit_sandbox)?;
let workspace = service.register_project(native_window_label, selected_fixture)?;
let snapshot = service.read_document(
    native_window_label,
    &workspace.workspace_id,
    &workspace.manifest.files[0].document_id,
)?;
// SaveDocumentRequest carries the snapshot revision/digest and the renderer's
// current editRevision. A save acknowledgement alone cannot clear newer edits.
```

The fixture sandbox must already exist below the application checkout's
`audit/migration/scratch`. The constructor walks and rejects symlink/reparse
components, then refuses every project/create parent outside that sandbox.
Production construction refuses the application checkout and requires explicit
selection. Nothing implicitly opens the working directory.

Enumeration reads only visible root `.ino`, `.cpp`, `.c`, `.h`, `.hpp` and `.txt`
files. It does not open child directories, protected caches or IDE metadata.
Strict scan/read errors remain errors. The default limits are 8 MiB per source,
32 MiB per complete manifest, 256 sources, 4,096 directory entries and 16
workspaces. Manifests hash actual bytes and retain document IDs across known
unchanged filenames. Removing/re-adding a file through an observed manifest
allocates a new document identity. Root `.ino` files sort first.

Reads preserve UTF-8 BOMs, Unicode and original newline bytes. Invalid UTF-8 fails
explicitly instead of silently replacing bytes. Save compares the caller's
known revision/digest and actual current disk bytes. Equal-content saves perform
no write, chmod or timestamp update. A changed save stages a create-new file in
the registered root, flushes it, retains file permissions, checks the original
bytes again, replaces the original, and verifies the resulting bytes before
acknowledging. The acknowledgement echoes `editRevision`; the frontend must
compare it with its current buffer before marking the tab clean. Revision
numbers remain within JavaScript's exact integer range.

`create_project` uses create-only folder/file ownership and the source templates
from `main/core/project_scaffold.py`. Checked headers add the exact quoted
include to generated `.ino` and optional `.cpp` files. Without a header, optional
`.cpp` is empty. Standard, bare and explicit Blink output remains distinct.
New scaffold files retain the legacy native newline convention: CRLF on Windows
and LF on Linux. Saving existing source content never translates newlines.
An existing target is never adopted or overwritten; an unsuccessful new
scaffold is retained for inspection rather than recursively cleaned.

Current safety limits are explicit: ownership is in-process only; legacy/new
applications do not yet share an OS lock. Standard-library path checks do not
eliminate adversarial symlink replacement between checking/opening, and an
unrelated writer can race the final compare/replace. A root replaced by another
directory at the same pathname is not distinguished by a persistent filesystem
identity. Source replacement does not preserve extended attributes, ACLs or
ownership metadata beyond ordinary permissions, and interrupted processes can
leave their own hidden staging file. These are release gates, not verified
capabilities. Do not enable writes to user projects until native directory-handle
and interprocess ownership work has resolved them.

Run from `desktop/` with the pinned local toolchain:

```sh
cargo test -p mcu-contracts -p mcu-core
cargo clippy -p mcu-contracts -p mcu-core --all-targets -- -D warnings
```

Tests create unique disposable trees only under `audit/migration/scratch` and
remove only their own trees. No GUI, hardware, real downloads, credentials,
settings or live cache generation is involved. Unix-specific filesystem cases
do not constitute native Windows evidence.
