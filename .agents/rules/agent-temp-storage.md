## Authorized Agent Working Directory (`temp/`)

The owner and user of this system has explicitly granted the agent **full, absolute control** over the workspace `temp/` directory (`<workspaceRoot>/temp/`):

- **Unrestricted Agent Storage:** The agent is authorized to store any number of files (including thousands of files) for current and future tasks without asking for confirmation.
- **Permitted Uses:** Scratch scripts, audit logs, benchmark traces, AST dumps, intermediate diffs, decompiled assets, and notes.
- **Git Protection:** The `temp/` directory is permanently gitignored and must never be tracked or bundled into releases.
- **Zero Touch for Test Fixtures:** Note that `test/` and `tests/` remain isolated test directories that require explicit user commands to touch, whereas `temp/` is fully open for autonomous agent operations.
