---
trigger: always_on
---

## Multi-User Deployment Awareness

This project (MCU Flasher by Naph) will be deployed across multiple Windows 10/11 machines, not just one or two dev PCs. Always consider:

- **No hardcoded paths, usernames, or drive letters.** Never assume `C:\Users\napht\...` or similar — resolve paths dynamically (e.g. `os.path.expanduser("~")`, `os.environ["USERPROFILE"]`, `sys.executable`, relative-to-script paths).
- **No hardcoded system state assumptions.** Don't assume a specific Python version, install location, PATH configuration, admin privileges, or pre-installed tools (arduino-cli, npm, winget, etc.) exist on the target machine — always detect/verify first, and handle the case where they don't.
- **Environment differences across machines.** Account for different Windows builds, differing user permission levels, differing locale/username character sets, and antivirus/PATH refresh quirks that may vary machine to machine.
- **Dynamic over static.** When implementing detection, install, config, or file-resolution logic, prefer dynamic discovery (registry lookups, `where`/`shutil.which`, querying installed versions, reading config at runtime) over fixed/static values baked into code.
- **Test the "first run on a fresh machine" scenario mentally** for any new feature — would this work on a clean Windows 10/11 install with a different username than `napht`?

**Agent enforcement:** Before finalizing any generated code or edit, scan for hardcoded user-specific paths (e.g. containing `napht`), fixed drive letters, or assumptions of a single-machine environment. If found, flag them explicitly and propose a dynamic alternative instead of silently leaving them in.