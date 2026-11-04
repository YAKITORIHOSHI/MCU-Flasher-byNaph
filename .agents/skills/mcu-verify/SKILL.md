---
name: mcu-verify
description: "Run MCU Flasher's hardware-free verifiers and separate new failures from pre-existing ones. Use after app changes, before a commit or release, or when a direct/verify_*.py check fails and you need to know whether your change caused it."
---

# Verify MCU Flasher changes

Run from the project root with the private runtime:

```bash
.venv-linux/bin/python3 -B direct/run_verifiers.py                # everything
.venv-linux/bin/python3 -B direct/run_verifiers.py --baseline HEAD
.venv-linux/bin/python3 -B direct/run_verifiers.py incremental upload reset
```

`--baseline <ref>` reruns each failure against a clean export of that ref and
reports only what the working tree newly broke. Always use it before calling a
failure pre-existing. Name fragments select verifiers by file name.

## Choosing checks

- Build, upload or reset changes: `incremental reset_build_cache board_build_cache upload`.
  `verify_incremental_builds.py` drives the real compile worker and PlatformIO.
  Mocked-compiler fixtures cannot see PlatformIO deleting objects.
- Bootstrap or package changes: `bootstrap offline package`.
- GUI changes: `runtime responsive controls performance shutdown`.
- Editor changes: `editor` plus the JavaScript `direct/verify_editor_*.js` checks.

## Reading results

- A verifier can pass every test and still exit 139: PySide6 can crash during
  interpreter teardown. Read the unittest summary, not only the exit code.
- A timeout under parallel load is not a failure until it repeats alone. Rerun
  with `--jobs 1 <name>`.
- No verifier may open a port, install anything or touch live caches. Windows,
  macOS and ARM results need their own hosts; a pass here does not qualify them.
