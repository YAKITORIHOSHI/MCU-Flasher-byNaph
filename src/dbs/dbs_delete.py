import json
import os
from .dbs_create import _DB_LOCK, _DB_PATH, _safe_replace_file

def clear_all_notifications() -> bool:
    """Clear all records from dbs_notif.json."""
    db_path = _DB_PATH
    with _DB_LOCK:
        try:
            temp_path = db_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump([], f, indent=2)
            _safe_replace_file(temp_path, db_path)
            return True
        except Exception as e:
            print(f"[dbs_delete] Failed to clear notifications: {e}")
            return False

def delete_notification(notif_id: str) -> bool:
    """Delete a specific notification record by ID."""
    db_path = _DB_PATH
    with _DB_LOCK:
        if not os.path.exists(db_path):
            return False
        try:
            with open(db_path, "r", encoding="utf-8") as f:
                records = json.load(f)
                if not isinstance(records, list):
                    return False
        except Exception:
            return False

        new_records = [r for r in records if r.get("id") != notif_id]
        if len(new_records) == len(records):
            return False

        try:
            temp_path = db_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(new_records, f, indent=2, ensure_ascii=False)
            _safe_replace_file(temp_path, db_path)
            return True
        except Exception as e:
            print(f"[dbs_delete] Failed to delete notification {notif_id}: {e}")
            return False

def delete_notifications_by_category(category: str) -> int:
    """Delete all notification records belonging to a category."""
    db_path = _DB_PATH
    with _DB_LOCK:
        if not os.path.exists(db_path):
            return 0
        try:
            with open(db_path, "r", encoding="utf-8") as f:
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
            temp_path = db_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(new_records, f, indent=2, ensure_ascii=False)
            _safe_replace_file(temp_path, db_path)
            return removed_count
        except Exception as e:
            print(f"[dbs_delete] Failed to delete category {category}: {e}")
            return 0
