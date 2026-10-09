"""Ubuntu assistant context preparation, isolated from Windows controllers."""
from pathlib import Path
import time


def prepare_context(backend, project, *, cancelled=lambda: False, timeout=30.0):
    """Wait for durable project context off Qt before an assistant can edit files."""
    from main.core.file_utils import ensure_hidden_read_first_md
    if cancelled():
        return False
    ensure_hidden_read_first_md(project)
    required = ("AGENTS.md", ".opencodeignore", ".opencode/skills/sketch-workflow/SKILL.md",
                ".opencode/skills/mcu-sketch-target/SKILL.md")
    if any(not (Path(project) / name).is_file() for name in required):
        raise RuntimeError("Project instructions could not be prepared. Retry after checking the project folder.")
    if cancelled():
        return False
    sync = getattr(backend, "_sync_project_hardware_state", None)
    if not callable(sync):
        return True
    requested = sync(project)
    lock = getattr(backend, "_hardware_state_lock", None)
    if not hasattr(lock, "__enter__"):
        return True
    deadline = time.monotonic() + timeout
    while not cancelled():
        with lock:
            running = backend._hardware_state_running
            durable = backend._last_synced_hardware_payload
        if not running:
            if requested and durable == requested:
                return True
            raise RuntimeError("The current project connection state could not be saved. Retry after checking the project folder.")
        if time.monotonic() >= deadline:
            raise RuntimeError("Project context is still being saved. Retry after the storage operation completes.")
        time.sleep(.025)
    return False
