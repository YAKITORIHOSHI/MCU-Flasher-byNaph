import json
import os
from .dbs_create import _DB_LOCK, _DB_PATH

def update_notification(notif_id: str, updates: dict) -> bool:
    """Update fields of an existing notification record in dbs_notif.json.

    Args:
        notif_id: The ID of the notification to update.
        updates: Dictionary of fields to update or add.

    Returns:
        True if updated successfully, False if not found or failed.
    """
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

        updated = False
        for record in records:
            if record.get("id") == notif_id:
                record.update(updates)
                updated = True
                break

        if not updated:
            return False

        try:
            temp_path = db_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2, ensure_ascii=False)
            os.replace(temp_path, db_path)
            return True
        except Exception as e:
            print(f"[dbs_update] Failed to update notification {notif_id}: {e}")
            return False
