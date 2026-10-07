"""Deliver separate downloader jobs through the existing backend notification bus."""
from __future__ import annotations

from src.modules.package_jobs import JobReader


class PackageActivityMonitor:
    def __init__(self, backend, stop, root=None):
        self.backend = backend
        self.stop = stop
        self.reader = JobReader(root)
        self.pending_refresh = False

    def poll(self):
        for snapshot, transitions in self.reader.read_updates():
            self.backend.emit("package:progress", snapshot)
            for transition in transitions:
                stage = transition["stage"]
                level = ("success" if stage == "ready" else "error" if stage in ("failed", "interrupted")
                         else "warning" if stage in ("unavailable", "cancelled") else "info")
                self.backend.emit("notification", {
                    "id": "package_" + snapshot["job_id"] + "_" + str(transition["seq"]),
                    "category": "board_install", "type": level,
                    "title": snapshot["title"], "message": transition["message"],
                    "details": {"job_id": snapshot["job_id"], "stage": stage,
                                **(snapshot.get("details", {}) if stage == snapshot["stage"] else {})},
                })
            if snapshot["stage"] in ("ready", "unavailable"):
                self.pending_refresh = True
        if (self.pending_refresh and not self.backend.is_busy
                and not getattr(self.backend, "_catalog_refresh_running", False)):
            accepted = self.backend.refresh_board_catalog(invalidate_parsed=True)
            self.pending_refresh = accepted is False

    def run(self):
        failed = False
        while not self.stop.is_set():
            try:
                self.poll()
                failed = False
            except Exception as exc:
                if not failed:
                    self.backend.emit("console:log", {"text": f"Package progress could not be read: {exc}",
                                                       "tag": "warning", "newline": True})
                    failed = True
            self.stop.wait(.5)
