---
name: agy-delegation
description: Use an installed, authenticated agy CLI as an additional assistant for explicitly requested MCU Flasher source investigations and documentation.
---

# Bounded agy delegation

Use `agy` for a narrow independent source investigation when the user requests
additional assistant work. Check local `agy --help` rather than assuming flags.
Prefer read-only plan mode, terminal sandboxing and a finite print timeout, for
example `agy --sandbox --mode plan --print-timeout 180s --print '<scoped task>'`.

Give the assistant the current workspace and AGENTS/skill boundaries, relevant
source filenames, a concrete question and the requested output. Exclude live
settings, hardware commands, protected caches/journals, installers and unrelated
directories. Keep ordinary tool permissions enabled; do not auto-approve or
bypass them. Parent Codex reviews findings and performs authorized changes and
verification. Delegate only work independent of the parent's active edits.

If authentication or workspace permissions are unavailable, stop the invocation
and report that limitation briefly. Continue using available Codex delegation;
do not initiate account login or repeatedly retry unattended OAuth.
