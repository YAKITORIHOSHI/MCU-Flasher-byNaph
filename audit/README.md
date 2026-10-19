# Migration audit

Updated: 19 October 2026, Asia/Manila. Current code checkpoint: `4cf730b` on
`codex/rust-tauri-migration`.

This folder records the ongoing Rust + Tauri 2 migration outside `temp/`.
Start with [migration status](migration/STATUS.md), [changes](migration/CHANGES.md)
and [restart handoff](migration/HANDOFF.md). The source/feature parity inventory
and reuse ledger are maintained alongside them. The Python application remains
the production release; the Rust prototype and ownership transactions remain
fixture-only. Implemented code does not imply complete parity or native release
qualification. The latest checkpoint preserves opaque settings JSON and exact
ownership timestamps, fixes AI-bar focus handoff, and verifies contained editor
viewports at both scales. [Status](migration/STATUS.md) distinguishes passing
fixtures, incomplete NVIDIA runs and the passing exact-source hosted checks.

Human-readable reports and machine-readable summaries are version-controlled.
Use exact source commits when reading [verification](migration/verification.json)
and [CI results](migration/ci-results.json); earlier passing results do not certify
later code. Large logs, captures and disposable fixture projects live under
`audit/migration/logs/`, `captures/` and `scratch/`. These directories are
ignored by Git but remain here for local inspection. Fixture projects contain no
live sketches, credentials or hardware operations.
