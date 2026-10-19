# Rust core migration boundary

`mcu-core` contains an initial project service and pure ports of preference
snapshot merging, serial text decoding, and compiler worker-budget decisions.
It also contains pure target-transport and legacy ownership-policy ports.
It is not a replacement for the current project controller, persisted settings,
serial device ownership, build orchestration, AI journals, cloud synchronization,
or editor recovery. The running Tauri preview must use `fixture`.

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

Current safety limits are explicit: the project service's ownership remains
in-process only. Separate host fixtures verify Python-compatible OS locking and
registration, but the project service is not wired to a live registration
transaction. Standard-library path checks do not
eliminate adversarial symlink replacement between checking/opening, and an
unrelated writer can race the final compare/replace. A root replaced by another
directory at the same pathname is not distinguished by a persistent filesystem
identity. Source replacement does not preserve extended attributes, ACLs or
ownership metadata beyond ordinary permissions, and interrupted processes can
leave their own hidden staging file. These are release gates, not verified
capabilities. Do not enable writes to user projects until native directory-handle
and interprocess ownership work has resolved them.

## Pure service ports

`preferences::merge_changes` ports the snapshot algorithm from
`main/core/config_store.py::merge_changes`: apply explicitly changed/deleted keys
while retaining another window's changes to unedited keys. Nested groups merge
recursively. Legacy Python equality between booleans, integral numbers and
floating-point equivalents is retained, including inside arrays and groups.
Inputs remain untouched. The Rust port reports incompatible group shapes as an
error requiring a fresh snapshot. It does not import or persist preferences,
open the current user's settings, migrate credentials, or implement Python's
cross-process `config_lock`. JSON numeric representation is also bounded by
`serde_json`, rather than Python's arbitrary-size integer representation.
Persistence, schema/import validation, rollback, and interoperable locking must
be qualified before this helper can replace a live settings transaction. The
host's isolated interoperability proof does not authorize this helper to open
the current user's settings.

`serial_text::SerialTextDecoder` ports
`main/core/serial_stream.py::SerialTextDecoder`. It preserves valid UTF-8 across
chunk boundaries, normalizes CRLF to one LF, renders a standalone CR as literal
`\x0d`, exposes each undecodable byte as `\xNN`, and distinguishes valid
Unicode controls using `\uNNNN`. ESC/BEL remain available to the display's
incremental control parser. Retained state is limited to an incomplete UTF-8
character of at most three bytes plus one pending CR; it does not retain device
history. This is a display decoder, not a raw serial transport or a firmware
source decoder. Port discovery, baud/line-ending settings, bounded queues,
generation identity, upload reservations, cancellation, disconnect recovery and
partial-write/no-replay rules remain unimplemented release gates. No decoder
check is evidence of USB or native serial compatibility.

`resource_budget::{safe_worker_count, optimal_compiler_jobs}` ports the decision
logic from `main/core/build_resources.py`, using metrics explicitly supplied by
the native host. It reserves logical CPU headroom, caps small four/six-core
physical CPUs, applies available-RAM and storage limits, and preserves the
LOW/MEDIUM/HIGH/MAX/ULTRA mode behavior. An optimal-job request maps modes other
than LOW/MEDIUM to MAX with at least 4 GiB available RAM and HIGH otherwise,
matching the reference. Unknown RAM, physical-core or storage metrics remain
unknown; the helper does not silently perform host discovery. Non-finite RAM
values return a structured calculation error rather than removing limits.
The host still must collect fresh metrics off the GUI thread, validate the
separate four-physical-core admission policy (logical fallback only when physical
count is unavailable), discover slow/removable/network storage, and pass the
selected settings mode. It also must own package/job scheduling and scoped child
environment variables. Unlike the Python module, importing this Rust module
does not probe the machine, load settings, or mutate process-wide build variables.
These pure calculations do not establish those missing host policies.

## Transport policy port

`target_transport` preserves the four pure helpers from
`main/core/target_profile.py`: whether a port is required, serial upload baud,
minimal upload configuration, and interface readiness. It retains all existing
programmer protocols, explicit manifest overrides, dynamic board identities,
non-ESP board defaults and the ESP request cap of 921600. Arduino CLI and native
programmer paths do not acquire an invented serial baud. The comparison harness
covers 13,039 cases, including explicit structural refusals for non-object target
inputs and truthy non-string INI protocol values. Those refusals differ from
Python's object representations intentionally and remain a compatibility boundary.

This is not a port of `target_problem`, exact target/preparation validation,
Arduino CLI proof, compiler configuration validation, tool orchestration or
hardware behavior. It is not exposed as permission to start an upload. The host
fixture lock proof separately verifies native interoperability with Python's
transaction lock, but does not publish legacy PID ownership or solve concurrent
live editing/store ownership. Keep these gates separate.

## Ownership policy and native fixture boundary

`ownership::{liveness, normalize_port, plan_claim}` uses the existing
`main/core/config.py` PID-keyed settings contract. Creation times are Unix epoch
seconds; a known process matches only when the absolute difference is strictly
less than two seconds. Missing cached inventory entries require a direct native
query. Unknown process evidence reserves the resource. Missing or malformed
caller evidence refuses registration, and a proven reused PID does not inherit
the previous process lifetime's project, port or preferences.

Plans preserve unrelated settings/records and update only the caller's claim.
Project comparison keys must come from the host, never the renderer. Project and
port collisions retain the original JSON. A process may keep or explicitly
release its workspace; claiming another active workspace requires release first.
This enforces the existing one-workspace-per-PID schema rather than inventing
per-window records. Non-ASCII numeric COM suffixes and malformed record shapes
are explicit compatibility refusals, not evidence that a resource is free.
The differential verifier covers 234 liveness, port-token and refusal cases.

`mcu-host::process_identity` queries one explicit PID without an inventory scan.
Linux uses bounded `/proc` records, native clock ticks and repeated start-time
checks. Windows uses an owned process handle, FILETIME conversion and
nonblocking exit checks. Only confirmed `Exited` evidence permits a stale-owner
decision. Every `Unknown` reason, including `ZombieOrDead`, reserves claims;
never map that reason to the core's confirmed `NotRunning` evidence by its name.

`mcu-host::fixture_registry` applies those plans under the interoperable OS lock
to fixed disposable settings beneath `audit/migration/scratch`. It freshly reads
settings, gathers controlled native evidence while locked, retains unknown
fields, stages changed bytes and detects noncooperating changes before rename.
Busy, colliding and unchanged plans do not rewrite settings. The native
registration verifier covers competing processes, live Python/native owners,
concurrent Python updates, release and reclaim after a confirmed child exit.
Its 21 Rust registry tests and nine native registration checks also cover exact
retention of large positive/negative Python integers, timestamps and nested
objects shaped like serde_json's private Number/RawValue markers.

The host registry uses a private validated JSON fragment representation:
`StreamDeserializer<IgnoredAny>` locates complete values without numeric
conversion, and string-key parsing plus fixed delimiters composes objects.
Only known ownership fields enter the core's typed policy projection; untouched
root, peer and caller metadata retain their original raw values. The composer
refuses changes outside the caller's ownership fields and preserves unknown
caller fields unless the core confirms PID reuse. `float_roundtrip` prevents
known creation-time rounding. This private fragment representation does not
require enabling serde_json's `raw_value` feature in the host dependency. Tauri
still enables `raw_value` transitively, so parsing unknown native metadata as generic `Value` can change
private-marker-shaped objects; this fragment boundary must remain in place.
It does not expand the generic preference helper's numeric/metadata guarantees.

This is fixture transaction evidence. It does not publish a production claim,
provide a source-save lease or resolve native file-identity/TOCTOU races, ACL
preservation, crash durability, package-store coordination, or activation
rollback. All native process/path observations and transactions belong on
workers, with fresh evidence inside the same future production transaction.

## Prototype differences and retirement gates

The project service deliberately has a narrower, stricter initial boundary.
These differences are recorded for qualification; they are not approval to
silently retire broader legacy behavior:

- The legacy editor listing uses a recursive, best-effort source helper in some
  paths. This service enumerates root files only, consistent with the root sketch
  compiler boundary, and rejects an incomplete scan. Nested legacy editor/project
  handling needs an explicit parity decision before replacement.
- The Rust source read rejects invalid UTF-8 instead of the legacy editor's lossy
  replacement decoding. Supported encoding/import and recovery behavior must be
  resolved without accidentally saving replacement characters over source bytes.
- Per-file, aggregate, entry and workspace bounds are new service limits. Their
  refusal states must be visible and qualified against real supported projects;
  current acceptance of a small fixture does not prove all projects are covered.
- Portable filenames reject case collisions, Windows device/ADS aliases and
  symlinks/reparse points on both hosts. Existing projects rejected by those
  stricter rules require an actionable compatibility/import path.
- Changed saves use staged replacement and preserve ordinary permissions while
  refusing read-only sources. The legacy save path can make files writable and
  retry transient writes. Rust has not reproduced that recovery behavior or
  certified native Windows replacement semantics. A content-equal save remains
  a no-op in both implementations.
- In-memory owner scopes, revisions and byte digests protect a single preview
  service only. Session IDs are not durable locks, persisted recovery records or
  authorization tokens. External writer races, directory replacement, native
  metadata preservation and legacy coexistence remain the filesystem gates
  described above.
- Project add/rename/delete, targeted AI source updates, tab/view/recovery state,
  cloud Pull coordination, user edit journals and save-before-build sequencing
  still belong to the legacy application. A manifest or save acknowledgement
  alone does not provide those workflows.

The Python implementation remains the behavioral reference. Each corresponding
application service stays available until its new host adapter and relevant
failure/recovery cases pass; these helpers do not justify removing Python code
used by PlatformIO or other approved firmware tools.

Run from `desktop/` with the pinned local toolchain:

```sh
cargo test -p mcu-contracts -p mcu-core
cargo clippy -p mcu-contracts -p mcu-core --all-targets -- -D warnings
```

The source-level differential scripts compare the original pure algorithms with
the compiled fixture probes without importing production host discovery:

```sh
cargo build -p mcu-host --bins
cd ..
python3 -B -W error desktop/scripts/verify-preferences-merge.py
python3 -B -W error desktop/scripts/verify-serial-codec.py
python3 -B -W error desktop/scripts/verify-resource-budget.py
python3 -B -W error desktop/scripts/verify-scaffold.py
python3 -B -W error desktop/scripts/verify-target-transport.py
python3 -B -W error desktop/scripts/verify-ownership.py
python3 -B -W error desktop/scripts/verify-lock-interop.py
python3 -B -W error desktop/scripts/verify-process-identity.py
python3 -B -W error desktop/scripts/verify-registry.py
```

The scaffold verifier uses disposable fixtures rather than live user projects.
The native process and registration verifiers require `psutil>=5.9,<8` in the
selected reference Python environment. They query only explicit fixture PIDs;
they do not import the production configuration module or scan the PID inventory.
Pure algorithm agreement is distinct from native GUI, filesystem, settings,
process-tree, serial or USB compatibility evidence.

At code checkpoint `d68b50a`, the local suite passes 90 Rust tests, 40 frontend
tests, 46 native webview checks and 23,909 pure comparison cases (1,335 preference
cases). Separate native proofs pass eight lock, six process and nine registration
checks. Hosted checks for that exact source revision remain pending.

Tests create unique disposable trees only under `audit/migration/scratch` and
remove only their own trees. No GUI, hardware, real downloads, credentials,
live settings or live cache generation is involved. Unix-specific filesystem cases
do not constitute native Windows evidence.
