import json
import os
from pathlib import Path
from .dbs_create import _DB_LOCK, _get_db_path, _safe_replace_file

def clear_all_notifications(db_path: str | Path | None = None) -> bool:
    """Clear all records from target notification database."""
    target_db = _get_db_path(db_path)
    with _DB_LOCK:
        try:
            temp_path = target_db + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump([], f, indent=2)
            _safe_replace_file(temp_path, target_db)
            return True
        except Exception as e:
            print(f"[dbs_delete] Failed to clear notifications from {target_db}: {e}")
            return False

def delete_notification(notif_id: str, db_path: str | Path | None = None) -> bool:
    """Delete a specific notification record by ID."""
    target_db = _get_db_path(db_path)
    with _DB_LOCK:
        if not os.path.exists(target_db):
            return False
        try:
            with open(target_db, "r", encoding="utf-8") as f:
                records = json.load(f)
                if not isinstance(records, list):
                    return False
        except Exception:
            return False

        new_records = [r for r in records if r.get("id") != notif_id]
        if len(new_records) == len(records):
            return False

        try:
            temp_path = target_db + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(new_records, f, indent=2, ensure_ascii=False)
            _safe_replace_file(temp_path, target_db)
            return True
        except Exception as e:
            print(f"[dbs_delete] Failed to delete notification {notif_id} from {target_db}: {e}")
            return False

def delete_notifications_by_category(category: str, db_path: str | Path | None = None) -> int:
    """Delete all notification records belonging to a category."""
    target_db = _get_db_path(db_path)
    with _DB_LOCK:
        if not os.path.exists(target_db):
            return 0
        try:
            with open(target_db, "r", encoding="utf-8") as f:
                records = json.load(f)
                if not isinstance(records, list):
                    return 0
        except Exception:
            return 0

        new_records = [r for r in records if r.get("category") != category]
        removed_count = len(records) - len(new_records)
        if removed_count == 0:
            return 0

        try:
            temp_path = target_db + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(new_records, f, indent=2, ensure_ascii=False)
            _safe_replace_file(temp_path, target_db)
            return removed_count
        except Exception as e:
            print(f"[dbs_delete] Failed to delete category {category} from {target_db}: {e}")
            return 0
