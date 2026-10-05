import json
import os
import time
from datetime import datetime
import threading
from uuid import uuid4
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
    """Replace a prepared sibling atomically; never truncate the old database."""
    source, target = Path(src), Path(dst)
    attempts = max(1, min(5, int(max_retries)))
    original_source_attrs = None
    hide_target = False
    if os.name == "nt":
        from main.core.file_utils import ensure_file_writable, hide_hidden_attribute, _set_windows_file_attributes
        try:
            original_source_attrs = source.stat().st_file_attributes
        except OSError:
            pass
        try:
            target_attrs = target.stat().st_file_attributes
        except OSError:
            target_attrs = 0
        hide_target = bool(target_attrs & 0x02) or \
            any(part.casefold() == ".mcu_flasher_build_cache" for part in target.parts) or target.name.startswith(".")
        if hide_target:
            hide_hidden_attribute(source)
        # The replacement inherits its sibling's attributes. Keep the old
        # hidden/system bits while leaving app-owned records writable.
        if target_attrs & 0x04:
            prepared_attrs = (original_source_attrs or 0x80) | (target_attrs & 0x06)
            if hide_target:
                prepared_attrs |= 0x02
            _set_windows_file_attributes(source, prepared_attrs & ~0x01)

    succeeded = False
    try:
        for attempt in range(attempts):
            try:
                if os.name == "nt":
                    ensure_file_writable(target)
                os.replace(src, dst)
                succeeded = True
                if os.name == "nt" and hide_target:
                    hide_hidden_attribute(target)
                return True
            except (PermissionError, OSError):
                if attempt < attempts - 1:
                    time.sleep((max(0, backoff_ms) * (2 ** attempt)) / 1000.0)
        return False
    finally:
        if not succeeded and original_source_attrs is not None:
            # A fixed staging filename may be reused by the next write.
            # Restore its original visibility after failed replacement so
            # Windows permits truncating that staging file again.
            _set_windows_file_attributes(source, (original_source_attrs & ~0x01) or 0x80)

def add_notification(
    category: str = "system",
    level: str = "info",
    title: str = "",
    message: str = "",
    details: dict | None = None,
    max_records: int = 500,
    db_path: str | Path | None = None,
    notification_id: str | None = None,
) -> dict | None:
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
        notification_id: Optional unique ID shared with the live UI event.

    Returns:
        The persisted notification dictionary object, or None if saving fails.
    """
    now = datetime.now()
    ts_sec = int(time.time())
    ms = now.microsecond // 1000

    notif_id = notification_id or f"notif_{ts_sec}_{ms}_{uuid4().hex}"
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
            if not _safe_replace_file(temp_path, target_db):
                print(f"[dbs_create] Failed to save notification to {target_db}: file replacement failed")
                return None
        except Exception as e:
            print(f"[dbs_create] Failed to save notification to {target_db}: {e}")
            return None

    return record
