---
name: test-sandbox-isolation
description: "Guidelines and enforcement for the test/, tests/, and temp/ sandbox directories. Use ONLY when the user explicitly requests running, writing, debugging, analyzing tests, or specifically directs access to temp/ or test/ directories."
---

# Test & Temp Sandbox Directory Isolation Skill

Use this skill **ONLY** when the user explicitly asks to run, create, update, or inspect tests, or explicitly references files within `temp/`, `test/`, or `tests/` (e.g., `@temp/...` or `@test/...`).

## Core Principle: Zero-Touch by Default

The `temp/`, `test/`, and `tests/` directories serve as private developer archives, sandboxes, and scratch test environments. By default:
1. **Never read, search, or inspect files in `temp/`, `test/`, or `tests/`** during regular feature development, bugfixing, refactoring, or project hygiene audits.
2. **Never modify, overwrite, move, or delete files in `temp/`, `test/`, or `tests/`** unless the user explicitly commands it in their prompt.
3. **Keep completely excluded from git:** `temp/`, `test/`, `tests/`, and scratch test patterns (like `___*.py`, `old-*`) must always remain in `.gitignore` and never be committed into the repository.

---

## When to Access `temp/`, `test/`, or `tests/`

Only access or touch `temp/`, `test/`, or `tests/` under one of these explicit conditions:
- The user prompts: "run tests", "execute unit tests", "check test results", or similar.
- The user explicitly points to a file in `temp/` or `test/` using `@temp/...`, `@test/...`, or `@tests/...`.
- The user explicitly instructs to write a test script, sandbox an experiment, inspect archived temp reports, or update a test fixture.

---

## Guidelines When Working with Temp & Test Files (On User Request)

When the user explicitly asks to work with tests or temporary files:
- **Sandbox Isolation:** Test and temporary scripts must not pollute production runtime paths (`src/dbs/`, `logs/`, or project root).
- **Mocking External Hardware:** Tests should mock COM ports, PlatformIO core downloads, and Tkinter GUI instances when possible to allow fast headless execution.
- **Cleanup After Verification:** If temporary test artifacts or directories are generated during a test run, ensure they are cleaned up or confined inside the ignored test directory.
