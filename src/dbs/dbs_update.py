import json
import os
from pathlib import Path
from .dbs_create import _DB_LOCK, _get_db_path, _safe_replace_file

def update_notification(notif_id: str, updates: dict, db_path: str | Path | None = None) -> bool:
    """Update fields of an existing notification record.

    Args:
        notif_id: The ID of the notification to update.
        updates: Dictionary of fields to update or add.
        db_path: Optional explicit path to the target dbs_notif.json file.

    Returns:
        True if updated successfully, False if not found or failed.
    """
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

        updated = False
        for record in records:
            if record.get("id") == notif_id:
                record.update(updates)
                updated = True
                break

        if not updated:
            return False

        try:
            temp_path = target_db + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2, ensure_ascii=False)
            _safe_replace_file(temp_path, target_db)
            return True
        except Exception as e:
            print(f"[dbs_update] Failed to update notification {notif_id} in {target_db}: {e}")
            return False
