---
name: agent-storage
description: "Guidelines and workflows for using the temp/ workspace directory as an authorized persistent/scratch storage folder with full absolute agent control."
---

# Agent Workspace Storage Skill (`temp/`)

This skill defines the workflows, structure, and operational protocols for utilizing the `temp/` directory located at the project workspace root (`<workspaceRoot>/temp/`).

## Absolute Control & Owner Authorization

As explicitly authorized by the computer owner and user:
- The agent has **full, absolute control** over the `temp/` directory.
- The agent is permitted to store, create, modify, inspect, read, search, delete, and manage files here freely without requiring interactive per-operation confirmations.
- The agent may store large volumes of data (thousands of files if necessary), including scratch scripts, audit logs, benchmark outputs, AST caches, intermediate diffs, decompiled assets, experimental mockups, and notes for current and future prompts.

---

## Workspace Directory Structure Conventions

To keep `temp/` orderly and easy to navigate across multi-step tasks, the following subfolder conventions are recommended:

- **`temp/scratch/`**: Temporary Python scripts, test snippets, and quick reproduction harnesses.
- **`temp/audit/`**: Diagnostic reports, syntax audit outputs, AST dumps, and dependency graphs.
- **`temp/benchmarks/`**: Performance metrics, memory/CPU profiling logs, and build timing measurements.
- **`temp/cache/`**: Transient intermediate calculation caches, download artifacts, or temporary index files.
- **`temp/notes/`**: Multi-turn planning scratchpads, architectural summaries, and working notes.

---

## Safety & Multi-User Deployment Rules

1. **Strict Git Exclusion:** `temp/` is permanently gitignored. Any file written to `temp/` remains strictly local and will never be committed or included in clean user releases.
2. **Dynamic Path Resolution:** Always resolve the path to `temp/` dynamically relative to the repository root or script location (e.g. `Path(__file__).resolve().parent / "temp"` or `Path.cwd() / "temp"`). Never hardcode drive letters or user profile paths.
3. **No Production Pollution:** Production runtime state must still go to designated folders (`src/dbs/`, `logs/`, etc.). Do not redirect user-facing configuration or production database files into `temp/`.
4. **Resilient File Handling:** On Windows, handle potential file locks gracefully when cleaning or updating files in `temp/`.
