# Development firmware-tool boundary

`platformio_compile.py` is a separate, settings-free entry for the first explicit
prepared AVR sample. It has no native build caller and does not replace the
shipping Python wrapper or retire an application service. No real firmware
build has been qualified through this entry.

The entry imports standard-library code and explicitly admitted PlatformIO
modules. It never imports MCU Flasher settings, GUI/backend modules,
`offline_mode`, `offline_runtime`, platform adapters or per-user store discovery.
Mbed, Zephyr, ESP-IDF and Arduino-Q adapters are outside this initial closure.
The exact target must be supplied by native policy; the tool has no target or
root defaults.

## Explicit invocation contract

The future native owner must supply its admitted private interpreter and each
absolute directory. This example documents the API; it is not permission to
dispatch a real tool before the remaining gates below pass.

```sh
"$PRIVATE_PYTHON" -I -S -B desktop/firmware-tools/platformio_compile.py \
  --project-root "$STAGED_PROJECT" \
  --core-root "$OWNED_OUTPUT/core" \
  --packages-root "$PREPARED_PACKAGES" \
  --platforms-root "$PREPARED_PLATFORMS" \
  --site-packages-root "$ADMITTED_SITE_PACKAGES" \
  --output-root "$OWNED_OUTPUT" \
  --jobs 1 \
  --platform platformio/atmelavr@5.1.0 \
  --board uno \
  --framework arduino
```

Every argument is required once as a separate name/value pair; abbreviations,
extra commands and caller-supplied environment options are refused. Jobs must
be a literal integer from 1 to 6. Native resource policy must choose a permitted
value within that cap; accepting a number does not certify CPU/RAM/storage
readiness. The example uses one worker.

All roots must already exist without dot aliases, symlinks or reparse ancestors.
The writable core must be exactly `output-root/core`, precreated by the native
owner. Project, packages, platforms, site-packages and output roots must be
pairwise disjoint, including ancestry. Package/platform locations are explicit
prepared inputs; they are not inferred as children of the writable core.
Protected path components `.mcu_flasher_build_cache`, `.pio`, `.vscode`, `.clangd`
and `.opencode` are refused lexically, case-insensitively, before path access.

The project must contain a real `sources` directory and exactly these bounded
`platformio.ini` bytes, including the final newline:

```ini
[platformio]
src_dir = sources
include_dir = sources

[env:native_compile]
platform = platformio/atmelavr@5.1.0
board = uno
framework = arduino
```

Rust remains responsible for complete source inventory, byte generation,
entrypoint validation and target/package identity. The Python entry checks this
config against the explicit target; it neither writes config nor merges sources.

## Admitted behavior

The entry checks `isolated`, `no_site` and `dont_write_bytecode` before
third-party imports. It defines UTF-8 stdout/stderr even for early refusals.
Appending only the supplied site-packages directory avoids `site.addsitedir`,
`.pth` startup and application checkout import paths. Those flags do not prove
that the executable itself is an admitted private runtime; native byte and
identity evidence must supply that authority.

Inherited app/Python/PlatformIO policy is discarded; only Windows OS loader
root variables are retained. Fixed environment values route writable core,
build output, library dependencies, caches, temporary files and home/XDG state
under `output-root`. Output directories are created or checked before importing
PlatformIO. `tempfile.tempdir` is pinned so a denied temporary allocation fails
there instead of searching user/system fallback directories. Prepared project,
package, platform and site input bytes/inventory remain separate.

The only PlatformIO command is the literal argument vector
`platformio run --project-dir <project-root> -j <jobs>`. There is no upload,
reset, update, target switch, arbitrary script or shell argument.

Public and private install entry methods return only an already-found package,
without invoking upstream install, lock, cleanup or dependency preparation.
Missing, force and URI/external sources refuse. Registry/symlink guards are
placed on their real owning mixins; update/uninstall and direct preparation
helpers refuse. The platform-specific install override is replaced as well so
its extra compatibility/preparation/uninstall paths cannot follow a successful
existing-package lookup. Native admission must certify all required prepared
dependencies separately; this deliberately skips automatic dependency repair.

Python audit hooks refuse sockets, urllib requests, application/GUI imports,
known preparation executables and serialized subprocess command strings. These
hooks are policy checks inside trusted admitted Python code, not an OS sandbox
against arbitrary package code or native child networking.

## Evidence and unfinished gates

Run the dependency-free source-isolated verifier with standard Python:

```sh
python -I -S -B -W error desktop/scripts/verify-tool-only-entry.py
```

Current local result: **47 checks**, including an exact fake BasePackageManager
base ordering, platform override, public/private package refusals, fixed argv,
UTF-8 streams, owned temporary allocation and denied-allocation behavior,
unchanged input bytes/inventory, protected paths, roots/jobs/config and startup
flag refusals. It copies the entry and builds fake PlatformIO modules only under
`temp/audit/native-compile-inputs`; it never imports installed PlatformIO or
dispatches a compiler, installer or hardware operation. Negative startup flags
are simulated after safe `-I -S -B` process startup so missing `-S` cannot execute
the host interpreter's installed `.pth` files. Receipt:
`temp/audit/native-compile-inputs/tool-only-entry.json`.

Static inspection of the installed private reference reports
`platformio/__init__.py:16` as `VERSION = (6, 2, 0)`. Exact observed source
interfaces, read without importing them:

| Source under `.venv-linux/lib/python3.14/site-packages/platformio/package/manager/` | Observed signature or resolution |
| --- | --- |
| `_install.py:44` | `install(self, spec, skip_dependencies=False, force=False)` |
| `_install.py:54` | `_install(self, spec, skip_dependencies=False, force=False, compatibility=None)` |
| `_update.py:86`, `:118` | `update(self, from_spec, to_spec=None, skip_dependencies=False)`; `_update(self, pkg, outdated, skip_dependencies=False)` |
| `_registry.py:28`, `_symlink.py:48` | `install_from_registry(self, spec, search_qualifiers=None)`; `install_symlink(self, spec)` |
| `base.py:51` | Registry and Symlink mixins precede Install in the base list; patching only Install cannot guard those earlier direct methods. |
| `platform.py:40` | `install(self, spec, skip_dependencies=False, force=False, project_env=None, project_targets=None)` adds work after `super().install`; the new entry bypasses that override. |

This static observation and fake behavior do not establish installed-runtime
provenance, compatibility of another version or a successful real build. Before
any real invocation, require complete interpreter/site/platform/framework/tool
byte admission and immutable lifetime, native OS/architecture/resource/storage
admission, finite child ownership/cancellation/drain/descendant cleanup, and
fresh output/input checks. `.pth` suppression applies to this entry; nested
Python command construction and startup remain unqualified.

Windows real builds remain unavailable: Python's Windows subprocess audit form
can be a serialized string, which the current list-only audit policy refuses.
Windows lock behavior, executable access and actual process/loader behavior
need separate native qualification. No local fake or future CI fixture result
closes these gates, BLD-01, upload/reset, GUI Compile integration or release
replacement.
