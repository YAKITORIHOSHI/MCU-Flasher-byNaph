"""Small, dependency-free phase timings for cold and warm Bootstrap logs."""
from contextlib import contextmanager
import time


@contextmanager
def timed_stage(log, label):
    """Report real elapsed time, including a failed phase, without a deadline."""
    started = time.monotonic()
    log(f"Bootstrap timing: {label} started")
    try:
        yield
    except BaseException:
        log(f"Bootstrap timing: {label} failed after {time.monotonic() - started:.1f} s")
        raise
    else:
        log(f"Bootstrap timing: {label} completed in {time.monotonic() - started:.1f} s")
