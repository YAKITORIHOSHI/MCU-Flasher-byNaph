import json
import os
import time
from datetime import datetime
import threading

_DB_LOCK = threading.Lock()
_DB_PATH = os.path.join(os.path.dirname(__file__), "dbs_notif.json")

def _get_db_path() -> str:
    return _DB_PATH

def add_notification(
    category: str = "system",
    level: str = "info",
    title: str = "",
    message: str = "",
    details: dict | None = None,
    max_records: int = 500
) -> dict:
    """Create and persist a new notification record in dbs_notif.json.

    Args:
        category: 'board_install', 'library_install', 'device', 'build', 'system', 'error'
        level: 'info', 'success', 'warning', 'error'
        title: Short title description
        message: Full notification text
        details: Optional dictionary containing metadata
        max_records: Maximum historical records to retain (default: 500)

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

    db_path = _get_db_path()

    with _DB_LOCK:
        records = []
        if os.path.exists(db_path):
            try:
                with open(db_path, "r", encoding="utf-8") as f:
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
            temp_path = db_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2, ensure_ascii=False)
            os.replace(temp_path, db_path)
        except Exception as e:
            print(f"[dbs_create] Failed to save notification: {e}")

    return record
