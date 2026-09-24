---
name: test-sandbox-isolation
description: "Guidelines and enforcement for the test/ and tests/ sandbox directories. Use ONLY when the user explicitly requests running, writing, debugging, analyzing tests, or specifically directs access to test/ directories."
---

# Test Sandbox Directory Isolation Skill

Use this skill **ONLY** when the user explicitly asks to run, create, update, or inspect tests, or explicitly references files within `test/` or `tests/` (e.g., `@test/...` or `@tests/...`).

## Core Principle: Zero-Touch for Test Fixtures
The `test/` and `tests/` directories serve as private developer test suites and test fixtures. By default:
1. **Never read, search, or inspect files in `test/` or `tests/`** during regular feature development, bugfixing, refactoring, or project hygiene audits.
2. **Never modify, overwrite, move, or delete files in `test/` or `tests/`** unless the user explicitly commands it in their prompt.
3. **Keep completely excluded from git:** `test/`, `tests/`, and scratch test patterns (like `___*.py`, `old-*`) must always remain in `.gitignore` and never be committed into the repository.

---

## Authorized Working Directory (`temp/`) Distinction
Note that the workspace `temp/` directory is **decoupled** from test isolation:
- The owner/user has explicitly authorized the agent to have full, absolute control over `temp/`.
- The agent may autonomously store, read, write, delete, and manage files (scratch scripts, benchmark dumps, intermediate artifacts, notes, etc.) inside `temp/` for current and future tasks.
- `temp/` is gitignored so files placed there never enter git history.

---

## When to Access `test/` or `tests/`
Only access or touch `test/` or `tests/` under one of these explicit conditions:
- The user prompts: "run tests", "execute unit tests", "check test results", or similar.
- The user explicitly points to a file in `test/` using `@test/...` or `@tests/...`.
- The user explicitly instructs to write a test script or update a test fixture.

---

## Guidelines When Working with Test Files (On User Request)
When the user explicitly asks to work with tests:
- **Sandbox Isolation:** Test scripts must not pollute production runtime paths (`src/dbs/`, `logs/`, or project root).
- **Mocking External Hardware:** Tests should mock COM ports, PlatformIO core downloads, and Tkinter GUI instances when possible to allow fast headless execution.
- **Cleanup After Verification:** If temporary test artifacts are generated during a test run, ensure they are cleaned up or confined inside the ignored test directory.
