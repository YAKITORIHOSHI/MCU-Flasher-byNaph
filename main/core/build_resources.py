"""Shared CPU/RAM budgets; no host toolchain installation code."""
import os

def _available_memory_gb() -> float | None:
    try:
        import psutil
        return psutil.virtual_memory().available / (1024 ** 3)
    except Exception:
        return None


def _system_reserved_cpu_count(total_cpus: int) -> int:
    """Reserve more UI/system headroom on midrange and faster processors."""
    total_cpus = max(1, int(total_cpus or 1))
    if total_cpus >= 4:
        return 2
    if total_cpus > 1:
        return 1
    return 0


def _resource_safe_worker_count(mode: str = "HIGH", total_cpus: int | None = None,
                                available_gb: float | None = None,
                                physical_cpus: int | None = None) -> int:
    """Return a build/background concurrency level that will not swamp small PCs.

    Compiler processes are memory-heavy, so CPU count alone is not a safe
    multiplier. Reserve two logical CPUs on systems with 4+ logical CPUs
    for the editor, terminal, serial monitor and OS, then cap workers by
    currently available RAM. Four/six-core CPUs also reserve two physical
    cores' worth of compiler concurrency, including systems with SMT.
    """
    from src.modules.runtime_resources import logical_cpu_count, physical_cpu_count
    cpus = max(1, int(total_cpus or logical_cpu_count() or 1))
    memory_gb = _available_memory_gb() if available_gb is None else available_gb
    cpu_budget = max(1, cpus - _system_reserved_cpu_count(cpus))
    if physical_cpus is None and (total_cpus is None or total_cpus == logical_cpu_count()):
        physical_cpus = physical_cpu_count()
    if physical_cpus:
        # SMT siblings share execution resources. Small physical CPUs need
        # headroom even when they advertise 8 or 12 logical threads.
        physical_budget = physical_cpus - 2 if physical_cpus <= 6 else physical_cpus
        cpu_budget = min(cpu_budget, max(1, physical_budget))
    if memory_gb is not None:
        if memory_gb < 0.5:
            memory_budget = 1
        elif memory_gb < 1.0:
            memory_budget = 1
        elif memory_gb < 2.0:
            memory_budget = max(1, min(2, int((memory_gb - 0.25) / 0.45)))
        elif memory_gb < 4.0:
            memory_budget = max(1, int((memory_gb - 0.5) / 0.35))
        else:
            # Ample RAM available (>=4GB free): aggressively scale workers with low per-job floor
            memory_budget = max(1, int((memory_gb - 0.5) / 0.28))
        cpu_budget = min(cpu_budget, memory_budget)

    # HDD Seek Penalty Optimization: If running on a mechanical spinning hard drive,
    # excessive parallel compiler workers cause severe head thrashing and disk queue saturation.
    # Cap compiler workers to max 2 on HDDs to ensure sequential I/O.
    try:
        from main.core.file_utils import is_drive_hdd
        if is_drive_hdd():
            cpu_budget = min(cpu_budget, 2)
    except Exception:
        pass

    normalized = str(mode or "HIGH").upper()
    if normalized == "LOW":
        return max(1, min(max(1, cpus // 2), cpu_budget))
    if normalized == "MEDIUM":
        return max(1, min(cpu_budget, max(2, (cpus + 1) // 2)))
    if normalized in ("ULTRA", "MAX", "MAXIMUM"):
        return max(1, min(cpu_budget, cpus, 32))
    if memory_gb is not None and memory_gb >= 4.0:
        return max(1, min(cpu_budget, cpus, 24))
    return max(1, min(cpu_budget, cpus, 12))


def get_optimal_compiler_jobs(mode: str | None = None) -> int:
    """Dynamically determine the best compiler concurrency using real-time available RAM and user settings."""
    if not mode:
        try:
            from main.core.config import _load_raw_config
            mode = _load_raw_config().get("shared", {}).get("cpu_multithreading", "HIGH")
        except Exception:
            mode = "HIGH"
    current_avail_gb = _available_memory_gb()
    norm_mode = str(mode or "HIGH").upper()
    if norm_mode in ("LOW", "MEDIUM"):
        effective_mode = norm_mode
    else:
        effective_mode = "MAX" if (current_avail_gb is not None and current_avail_gb >= 4.0) else "HIGH"
    return _resource_safe_worker_count(effective_mode, available_gb=current_avail_gb)


_max_cpu_jobs = str(_resource_safe_worker_count("HIGH"))

os.environ["PLATFORMIO_BUILD_JOBS"] = _max_cpu_jobs
os.environ["PLATFORMIO_RUN_JOBS"] = _max_cpu_jobs
os.environ["PLATFORMIO_SETTING_ENABLE_CACHE"] = "true"
os.environ["SCONSFLAGS"] = f"-j{_max_cpu_jobs}"


__all__ = ["_available_memory_gb", "_system_reserved_cpu_count", "_resource_safe_worker_count", "get_optimal_compiler_jobs", "_max_cpu_jobs"]
