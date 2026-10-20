# Configured-index compile qualification

This opt-in developer campaign qualifies the current Python application's board
catalog with real PlatformIO Arduino sample builds. It does not upload, reset,
open a serial port, replace the application, or certify physical hardware.
The audit runner currently executes on Linux only; Windows application CI is a
separate integration check. A Windows campaign needs its own qualified process
ownership and cleanup implementation before this runner can execute there.

The inventory uses every explicitly supplied configured index URL. It selects the
latest stable release of each source package/architecture, checks the archive's
declared size/checksum, and reads every visible `boards.txt` declaration. Older
package versions and menu-option permutations are outside this inventory.
Archives are removed after metadata extraction. Unrelated archive links are
ignored; selected metadata links and escaping paths are refused.

The manifest requires a unique exact normalized board ID or name, an Arduino
framework, and an exact PlatformIO platform release. Shared chips, CPU families,
variants, USB identifiers or defines alone cannot certify board equivalence.
Unresolved and ambiguous declarations remain explicit skips; a brief registry
record cannot prove that no compatible target exists. There is no generic-board
substitution or Arduino CLI fallback. Duplicate declarations compile once per
exact platform/board/framework. Installed manifest hashes bind enriched matches
to the definitions actually compiled.

Official platform packages are preferred. An official-owner HTTP 404 permits a
bounded registry search only when it establishes one unique exact package owner.
The fetched metadata must confirm that identity. Prereleases remain explicitly
experimental and retain their owner/search/release provenance.

Example, from the checkout root with the private runtime:

```bash
.venv-linux/bin/python3 -B direct/board_compile_indexes.py \
  --index https://downloads.arduino.cc/packages/package_index.json \
  --index https://espressif.github.io/arduino-esp32/package_esp32_index.json \
  --scratch audit/migration/scratch/example-indexes \
  --output audit/migration/scratch/example-inventory.json
.venv-linux/bin/python3 -B direct/board_compile_manifest.py \
  --inventory audit/migration/scratch/example-inventory.json \
  --registry audit/migration/scratch/registry.json \
  --output audit/migration/scratch/example-targets.json
.venv-linux/bin/python3 -B direct/board_compile_campaign.py \
  --manifest audit/migration/scratch/example-targets.json \
  --scratch audit/migration/scratch/example-builds --jobs 12 \
  --reserve-gib 5 --max-owned-gib 6
```

`registry.json` must be a complete normalized PlatformIO board catalog. Optional
`--platform-core` enriches it from an explicit isolated metadata-only scratch
core; never point it at a live package store. Configured URLs are read separately
without invoking application discovery or rewriting settings.

Each target gets a fresh project containing only Arduino `setup()` and `loop()`.
Receipts retain commands, durations, installed package identities, board/platform
manifest hashes and actual firmware artifact sizes/hashes. Bounded compressed
logs survive cleanup. Timeout, storage deferral, interruption and compiler
failure remain separate outcomes. Resume requires matching recorded inputs and
tool identity; resumed passes are counted separately from new compiles.

The campaign isolates PlatformIO stores, HOME, XDG directories and temporary
files under its marked scratch directory. Completed project/build outputs and
platform batches are removed only through owned cleanup boundaries.
Linux child leaders remain unreaped until their captured process groups are
cleaned, including descendants surviving a successful leader. Cleanup escalates
within a deadline and refuses a child identity reaped by another caller. Scratch
ancestors cannot be symlinks. These are audit boundaries, not a sandbox against
package code deliberately escaping its session; community package qualification
also uses an external read-only host sandbox with only its owned scratch writable.

Use separate
MCU batches for large ESP32 packages: stock PlatformIO retains downloaded package
archives even when its HTTP content cache is disabled. The defaults preserve
5 GiB free space and limit owned growth to 6 GiB; a resource stop is incomplete
qualification, never a successful compile.

Offline guard checks run with:

```bash
.venv-linux/bin/python3 -B -W error direct/verify_board_compile_campaign.py
```

These direct compiler results complement the application's actual Compile-button
fixtures and native CI. Release qualification also needs those integration
checks, packaging and supported desktop/hardware workflows. The native Rust
replacement has its own [unfinished plan](../audit/migration/NATIVE_RUST_PLAN.md).
