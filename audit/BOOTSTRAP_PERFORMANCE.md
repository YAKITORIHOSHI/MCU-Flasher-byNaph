# Bootstrap performance correction — October 23, 2026

Implementation: `7973c67`; native fixture correction: `c10bf07`, on
`codex/rust-tauri-migration`.

The reported Windows run exceeded thirty minutes on stable Wi-Fi. Its log and
storage type were unavailable, so this correction does not establish that run's
exact bottleneck or a new complete setup duration.

## Confirmed delays and changes

Python package downloads disabled pip's progress display but killed a child
after 45 seconds without stdout. Installs had a similar 90-second silence limit.
Quiet healthy work could therefore be stopped and repeated, including another
online install and repair. The new monitor uses finite monotonic budgets, pip's
network timeouts and explicit cancellation. Qt downloads, installs and updates
receive a 900-second budget. Smaller package budgets remain finite and shorter.
Logs retain the actual stop reason, elapsed time and bounded, redacted output.

The monitor needs only the standard library, including before psutil is installed.
Windows children start suspended, join an owned kill-on-close Job, then resume
through pinned handles whose process ownership is checked. POSIX children use an
owned session whose leader identity remains reserved until teardown. Cancellation,
deadlines and an inherited pipe after parent exit stop owned descendants. Cleanup
failure retains ownership and waits only for teardown; after confirmed teardown
it remains a terminal error and cannot trigger an installer retry.

Package publication formerly copied the source, hashed the source separately and
hashed/synchronized staging. Eligible POSIX packages now hash the source copy
stream, then independently hash/synchronize staging. They also check source file
identities and inventory before/after copying and before publication. This detects
ordinary concurrent changes in installer-owned staging; it is not an immutable
snapshot against a process restoring every native metadata field.

Streaming requires at least 16 MiB total and 256 KiB average regular-file size.
Windows and custom copiers retain their original native copy and two-hash path,
including existing alternate-stream/attribute behavior. Small POSIX packages use
the original copier after a metadata eligibility walk. All paths retain absent-
destination publication, interrupted staging and independent staged verification.

Phase logs distinguish the Windows seed download/checksum, import/publication,
package planning, manager work, builders, catalog and Arduino coverage. A complete
store still skips the seed and certified packages retain coverage-only refresh.
The default AVR/ESP32/ESP8266 Arduino variants and upload/debug coverage are intact.

## Remaining first-run cost

The fixed Windows seed is 1,785,358,455 bytes (about 1.79 GB). At an illustrative
1 MiB/s that download alone takes about 28.4 minutes, before extraction. That is
arithmetic, not an observed speed on the reported PC. Stable Wi-Fi does not measure
the CDN connection, storage writes, extraction or antivirus scanning. Seed policy,
checksum validation, resumable checkpoints and package provenance are unchanged.
Native Ubuntu uses its own packages and never imports Windows toolchains.

## Local verification

Host: native Ubuntu 26.04 x86_64; private Python 3.14.4; PlatformIO 6.2.0;
PySide6 6.12.0. No live installers, hardware or protected caches were used.

| Suite | Cases | Native-only skips |
| --- | ---: | ---: |
| Pip monitor, real quiet/deadline/cancelled children and isolated integration | 23 | 3 Windows |
| Package recovery/publication | 23 | 1 Windows |
| Phase timing | 3 | 0 |
| Existing runtime | 46 | 0 |
| Existing resource performance | 32 | 1 |
| Seed | 26 | 1 Windows |
| Builders | 20 | 0 |
| Pip paths | 6 | 2 Windows |
| Bootstrap interruption | 13 | 0 |
| Ubuntu Bootstrap | 30 | 0 |
| Ubuntu ARM Bootstrap contracts | 19 | 0 |
| Cold Python discovery cache | 1 | 0 |
| Process boundaries | 26 | 1 Windows |
| Archive boundaries | 11 | 2 Windows |
| Package updates | 16 | 0 |

Every listed locally executable case passed. ARM contract fixtures on x86_64 are
not native ARM qualification. Changed Python sources compile, local AGENTS guidance
matches the template without live metadata generation, installer bytes match all
seven committed Git LFS receipts, and Git integrity passes.

Warm local benchmark: three alternating trials per policy, owned 32 MiB fixtures.

| Workload | Previous median | Current median | Logical payload reads |
| --- | ---: | ---: | --- |
| 5 regular files | 0.0703 s | 0.0707 s | 96 MiB → 64 MiB |
| 4097 regular files | 3.2748 s | 3.3272 s | 96 MiB → 96 MiB |

This establishes a large-payload logical-read reduction, with no demonstrated
wall-time improvement on the cached local SSD. It does not establish physical disk
I/O, a cold HDD/USB result, Windows throughput or a network download speedup.

## Native evidence

The targeted `bootstrap-performance.yml` matrix runs Windows Python 3.11/3.14
and Ubuntu 24.04/26.04 AMD64/ARM64. It uses disposable fixtures, synthetic children
and an I/O benchmark, without downloading board toolchains or running installers.
All six jobs passed for `c10bf07` in the
[native performance run](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39960413788):
Windows Server 2025 AMD64 with Python 3.11 and 3.14, and native Ubuntu 24.04/26.04
on AMD64 and ARM64. The first matrix's new monitor/publication checks also passed;
its remaining failure was an older relative-path fixture that assumed drive C:
while the Windows checkout was on D:. That fixture now checks the native error
as well as the native result, without changing production path handling.

These native checks cover owned child cleanup, quiet work, cancellation, package
integrity, archive path handling and the local I/O benchmark. They are not a
complete cold Windows Bootstrap or physical Windows 10/11 desktop qualification.

Broader suites on the earlier `be60163` were not wholly green. The desktop suite
reported outdated Ubuntu launcher fixtures (missing fixture LAUNCHER directory
and a removed root ARM launcher), and a Windows moved-venv/cscript fixture exceeded
its 30-second limit. ARM jobs failed their host-contract step while their separate
empty-runtime/full-package/ESP32-compile step passed. Those outcomes are retained
as broader qualification gaps; this focused run does not relabel them as passes.

The locally built Bootstrap-only archive is based on `c10bf07` and passed every
per-file hash, fresh-default and launcher check. It contains all 17,496 private
Windows runtime files (1,265,081,823 bytes) and seven verified hydrated installers.
Subsequent responsive-layout work requires a new combined archive; this archive
does not include it. No GitHub release asset was published.
