# Configured-index compile qualification — 20 October 2026

**318/318 exact PlatformIO targets produced real sample firmware artifacts.** Build-only qualification passes on Ubuntu 26.04 AMD64, including experimental UNO Q in the original checkout path containing spaces after the `f249c21` compatibility repair. The current Python application is **not cleared for unrestricted deployment** pending actual user desktop, device/upload/reset/USB and installation qualification. The complete Rust replacement remains unfinished.

The source inventory covers both configured URLs (official Arduino and the saved Espressif index), latest stable releases per package/architecture, 37 package entries (36 unique releases), 949 visible rows and 543 distinct declarations. Exact ID/name and single-candidate resolution makes 601 rows / 330 distinct declarations eligible, deduplicated to 318 target builds. **213 distinct declarations were skipped**: 180 unresolved and 33 ambiguous. Shared chips, USB IDs, variants and defines never substitute another board. There was no Arduino CLI fallback.

| Exact pinned platform | Passed targets |
| --- | ---: |
| `lee-lab-skku/arduinoq@1.0.0-rc.4` | 1 |
| `platformio/atmelavr@5.3.0` | 17 |
| `platformio/atmelmegaavr@1.10.0` | 2 |
| `platformio/atmelsam@9.0.0` | 14 |
| `platformio/espressif32@8.0.0` | 271 |
| `platformio/intel_arc32@1.3.0` | 1 |
| `platformio/nordicnrf52@11.0.0` | 2 |
| `platformio/raspberrypi@1.20.0` | 2 |
| `platformio/renesas-ra@1.9.0` | 4 |
| `platformio/ststm32@20.0.0` | 4 |

Onehorse exposed missing DIO boot/SDK selection in its DOUT recipe. MiniMain ESP32-S2 exposed an invalid hardware-USB mode. Both pass after narrowly versioned repairs shared by the actual Python INI generator and campaign. The helper verifies exact board bytes, platform dispatch and framework/SDK identity, preserves board IDs/variants and never edits installed packages.

UNO Q is an experimental community prerelease for native Linux. Its original path-with-spaces linker defect is **closed**. The production compatibility selector now verifies exact platform/board/builder hashes and framework identity before generating the Linux-only `post:` hook `src/modules/arduinoq_link_paths.py`. The hook changes only the two known `-T` linker-script operands to SCons File nodes, preserving each path as one argument. Its SHA256 is part of generated INI inputs so hook changes invalidate firmware reuse. Installed package files are untouched. The hook is approved firmware-tool Python; its application selection/ownership policy remains to be ported to Rust.

The fresh original-path receipt is [`unoq-app-hook-batch/results.jsonl`](scratch/board-campaign-20261019/unoq-app-hook-batch/results.jsonl). It retains the preparation failure and successful build-only compatibility retry under the original checkout path, with real linker, strip, pack and size-check execution. The former Bubblewrap no-space alias pass remains historical evidence only. The output includes a 5,376-byte `firmware.elf` (SHA256 `be303327441e38af07663d396c7e8901c8e5b96e9e864a2a062e2410df690ae7`) and packed sketch. It is a Zephyr LLEXT extension requiring resident firmware, not a standalone complete-device image. No Windows or device/upload support is qualified by this Linux build.

At `f249c21`, fifteen local isolated recipe checks cover exact identities, path handling, hook selection, shared application generation and unchanged INI timestamps; Windows deliberately skips three Linux-only UNO Q checks. The subsequent `8dbdb9d` reproduces and fixes a FIFO metadata-reader stall with bounded reads from verified regular-file descriptors. All sixteen local recipe checks then pass, with four Linux-only cases expected to skip on Windows. Five Python-release source-selection checks also pass. This hardening does not represent a fresh 318-target compile rerun; every real compile keeps its actual command and source receipts.

Thirteen campaign guard checks, 46 runtime checks and 20 responsive checks pass at their recorded source. SDK checkpoint `77e5ccb` passes all five migration CI jobs in run 39357769519. The hook checkpoint `f249c21` also passes all five focused migration jobs in run 39359319887. Focused CI for `8dbdb9d` passes all five jobs in run 39360040590: 16 recipe checks (four expected Windows skips) and 17 native widget checks on each host.

Every passing result includes the real command (`--jobs 12`), exact board/platform hashes, installed package identities and generated artifact sizes/SHA256. Initial failures, interrupted/storage-deferred campaigns and superseded mappings remain retained separately. Earlier passes are reused only after current target-key and installed-manifest hash checks; no resumed result is presented as a newly executed compile.

Storage policy used a 5 GiB stop threshold and a 6 GiB per-batch growth cap (2 GiB for UNO Q). Large ESP32 families compiled sequentially. All owned campaign work/package trees, including the original-path hook retry, were removed; bounded logs, metadata and receipts remain. Approximately 10.9 GiB was free after completion and metadata pruning. User stores, sketches, settings and protected caches/journals were untouched.

The [machine-readable report](board-compile-campaign.json) records every target, skip, package set and receipt hash. The [method](../../direct/BOARD_COMPILE_CAMPAIGN.md) and [native Rust plan](NATIVE_RUST_PLAN.md) describe reproduction and remaining gates. Compile success does not certify actual upload/reset/USB, the full app Compile integration for every target, packaging or complete native Rust backend parity.
