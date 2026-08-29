import json
import os
import time
from datetime import datetime
import threading
from pathlib import Path

_DB_LOCK = threading.Lock()
_FALLBACK_DB_PATH = os.path.join(os.path.dirname(__file__), "dbs_notif.json")
_CURRENT_DB_PATH = _FALLBACK_DB_PATH

def set_default_db_path(path: str | Path | None) -> None:
    """Set the default JSON database path used when no explicit db_path is passed."""
    global _CURRENT_DB_PATH
    with _DB_LOCK:
        if path:
            _CURRENT_DB_PATH = str(Path(path).resolve(strict=False))
        else:
            _CURRENT_DB_PATH = _FALLBACK_DB_PATH

def get_default_db_path() -> str:
    """Return the current default database path."""
    with _DB_LOCK:
        return _CURRENT_DB_PATH

def _get_db_path(explicit_path: str | Path | None = None) -> str:
    if explicit_path:
        return str(Path(explicit_path).resolve(strict=False))
    return get_default_db_path()

def _ensure_parent_dir(db_path: str) -> None:
    """Ensure parent directory exists and is hidden if inside build cache."""
    try:
        p = Path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

def _safe_replace_file(src: str, dst: str, max_retries: int = 5, backoff_ms: int = 50) -> bool:
    for attempt in range(max_retries):
        try:
            if os.path.exists(dst) and os.name == "nt":
                try:
                    os.chmod(dst, 0o666)
                except Exception:
                    pass
            os.replace(src, dst)
            # Apply hidden attribute on Windows if in cache directory
            if os.name == "nt" and (".mcu_flasher_build_cache" in dst or os.path.basename(dst).startswith(".")):
                try:
                    import ctypes
                    ctypes.windll.kernel32.SetFileAttributesW(str(dst), 0x02)  # FILE_ATTRIBUTE_HIDDEN
                except Exception:
                    pass
            return True
        except (PermissionError, OSError):
            if attempt < max_retries - 1:
                time.sleep((backoff_ms * (2 ** attempt)) / 1000.0)
            else:
                try:
                    import shutil
                    shutil.copy2(src, dst)
                    if os.path.exists(src):
                        os.unlink(src)
                    if os.name == "nt" and (".mcu_flasher_build_cache" in dst or os.path.basename(dst).startswith(".")):
                        try:
                            import ctypes
                            ctypes.windll.kernel32.SetFileAttributesW(str(dst), 0x02)
                        except Exception:
                            pass
                    return True
                except Exception:
                    return False
    return False

def add_notification(
    category: str = "system",
    level: str = "info",
    title: str = "",
    message: str = "",
    details: dict | None = None,
    max_records: int = 500,
    db_path: str | Path | None = None,
) -> dict:
    """Create and persist a new notification record.

    Args:
        category: 'board_install', 'library_install', 'device', 'build', 'system', 'error'
        level: 'info', 'success', 'warning', 'error'
        title: Short title description
        message: Full notification text
        details: Optional dictionary containing metadata
        max_records: Maximum historical records to retain (default: 500)
        db_path: Optional explicit path to the target dbs_notif.json file.
                 If None, uses current default/active project database.

    Returns:
        The created notification dictionary object.
    """
    now = datetime.now()
    ts_sec = int(time.time())
    ms = now.microsecond // 1000

    notif_id = f"notif_{ts_sec}_{ms}"
    record = {
        "id": notif_id,
        "timestamp": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "category": category,
        "level": level,
        "title": title or category.replace("_", " ").title(),
        "message": message,
        "details": details or {}
    }

    target_db = _get_db_path(db_path)
    _ensure_parent_dir(target_db)

    with _DB_LOCK:
        records = []
        if os.path.exists(target_db):
            try:
                with open(target_db, "r", encoding="utf-8") as f:
                    records = json.load(f)
                    if not isinstance(records, list):
                        records = []
            except Exception:
                records = []

        records.append(record)

        # Enforce max storage limit (keep latest records)
        if len(records) > max_records:
            records = records[-max_records:]

        try:
            temp_path = target_db + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2, ensure_ascii=False)
            _safe_replace_file(temp_path, target_db)
        except Exception as e:
            print(f"[dbs_create] Failed to save notification to {target_db}: {e}")

    return record
