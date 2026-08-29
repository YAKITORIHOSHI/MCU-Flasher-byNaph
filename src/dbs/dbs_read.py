import json
import os
from pathlib import Path
from .dbs_create import _DB_LOCK, _get_db_path

def get_notifications(
    category: str | None = None,
    level: str | None = None,
    limit: int | None = 100,
    search_query: str | None = None,
    db_path: str | Path | None = None,
) -> list[dict]:
    """Query notifications from dbs_notif.json.

    Args:
        category: Filter by specific category (e.g., 'board_install', 'library_install')
        level: Filter by severity level ('info', 'success', 'warning', 'error')
        limit: Max number of recent records to return (None for all)
        search_query: Search string to match in title or message
        db_path: Optional explicit path to the target dbs_notif.json file.

    Returns:
        List of matching notification dicts ordered newest first.
    """
    target_db = _get_db_path(db_path)

    with _DB_LOCK:
        if not os.path.exists(target_db):
            return []
        try:
            with open(target_db, "r", encoding="utf-8") as f:
                records = json.load(f)
                if not isinstance(records, list):
                    return []
        except Exception:
            return []

    # Newest first
    results = list(reversed(records))

    if category and category != "all":
        results = [r for r in results if r.get("category") == category]

    if level and level != "all":
        results = [r for r in results if r.get("level") == level]

    if search_query:
        query_low = search_query.lower()
        results = [
            r for r in results
            if query_low in r.get("title", "").lower()
            or query_low in r.get("message", "").lower()
            or query_low in r.get("date", "")
            or query_low in r.get("time", "")
        ]

    if limit is not None and limit > 0:
        results = results[:limit]

    return results

def get_notification_by_id(notif_id: str, db_path: str | Path | None = None) -> dict | None:
    """Find a specific notification record by ID."""
    target_db = _get_db_path(db_path)
    with _DB_LOCK:
        if not os.path.exists(target_db):
            return None
        try:
            with open(target_db, "r", encoding="utf-8") as f:
                records = json.load(f)
                for r in records:
                    if r.get("id") == notif_id:
                        return r
        except Exception:
            pass
    return None
