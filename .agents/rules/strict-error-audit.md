## Strict Pre-Completion Error & Warning Audit & Mandatory Tasks

The agent MUST make the following an absolute must and highest critical task on EVERY prompt without exception:

### 1. Mandatory Pre-Completion Audit Protocol (Zero Errors & Warnings)
The agent MUST NOT conclude any response or jump to any next prompt until thoroughly verifying all touched files for errors and warnings:
1. **Compilation Check:** Proactively compile all modified Python files (`python -m py_compile <files>`) to ensure zero syntax errors and valid bytecode.
2. **Variable & Import Scope Verification:** Verify that all variables referenced in f-strings, local scopes, and exception blocks are defined and properly imported. Never leave undefined variables (e.g. Pyright `reportUndefinedVariable`).
3. **No Unresolved Diagnostics:** If any error, warning, syntax issue, or linter diagnostic is found, resolve it immediately within the same turn before concluding.
4. **Execution of Active Verification:** The agent must execute an active verification tool call before declaring completion.

### 2. Headless Assistant Delegation and No Stray GUI Windows
Antigravity must never open a window unless the user explicitly asks to use the IDE. Do not launch `agy` interactively or with `--dangerously-skip-permissions`, and never launch `antigravity-ide` for diagnostics or verification. For explicitly requested assistant delegation, follow `.agents/skills/agy-delegation/SKILL.md`: check `agy --help`, then run read-only plan mode with sandboxing, `--print` and a finite timeout so the command stays headless. Run GUI checks only in isolated owned fixtures, never on the developer's desktop. If headless execution is unavailable, stop and report the limitation instead of opening the GUI.

### 3. Clean and Remove Unused Imports
Always check for unused imports across all touched/modified files. Either clean them up or remove them entirely. Never leave unused imports behind in code under any circumstances.

