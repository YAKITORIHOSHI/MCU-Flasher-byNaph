"""Operation-owned USB loss detection; never retry or replay a hardware command."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import time

from main.core.target_profile import requires_upload_port


class OperationConnectionLost(ConnectionError):
    """The captured hardware transport disappeared during this operation."""


def expects_serial_handoff(info: dict) -> bool:
    """Read explicit 1200-baud/bootloader handoff declarations for this target."""
    upload = info.get("upload") or {}
    declarations = [upload] if isinstance(upload, dict) else []
    declarations.append(info)
    manifest = info.get("pio_manifest") or info.get("manifest")
    if manifest:
        try:
            path = Path(manifest)
            if path.is_file() and path.stat().st_size <= 1024 * 1024:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("upload"), dict):
                    declarations.append(data["upload"])
        except (OSError, ValueError, TypeError):
            pass
    for row in declarations:
        for name in ("use_1200bps_touch", "wait_for_upload_port",
                     "upload.use_1200bps_touch", "upload.wait_for_upload_port"):
            value = row.get(name)
            if value is True or str(value).casefold() in ("1", "true", "yes"):
                return True
    return False


def record_connection_loss(api, port: str) -> str:
    """Latch one reason for this session before any subsequent write boundary."""
    reason = str(getattr(api, "_operation_connection_loss", "") or "")
    if not reason:
        reason = f"The MCU connection on {port} was lost."
        api._operation_connection_loss = reason
        api.emit("console:log", {
            "text": reason + " The operation was halted and will not resume automatically.",
            "tag": "error", "newline": True,
        })
        api.emit("notification", {
            "title": "MCU connection lost", "type": "error",
            "message": reason + " Reconnect the MCU and start the action again. If erase or writing began, firmware may be incomplete.",
        })
    return reason


def check_write_connection(api, port: str, info: dict) -> None:
    """A lost selection during preparation must not enter a later hardware phase."""
    reason = str(getattr(api, "_operation_connection_loss", "") or "")
    if reason:
        raise OperationConnectionLost(reason)
    if port and requires_upload_port(info) and getattr(api, "current_port", port) != port:
        raise OperationConnectionLost(record_connection_loss(api, port))


class SerialConnectionGuard:
    """Confirm serial disappearance off the GUI thread and reap its exact child.

    Ordinary compile and nonserial programmers do not use this guard. A declared
    USB handoff gets ten seconds to enumerate again, including the same USB
    serial number/location under a new port name. Enumeration failures are
    unknown state, never evidence that the MCU disappeared.
    """

    def __init__(self, api, process, port: str, info: dict, *, expected_handoff=False,
                 scan=None, clock=None, confirm_after=1.5, handoff_after=10.0,
                 scan_interval=0.5):
        self.api, self.process, self.port = api, process, str(port or "")
        self.session = getattr(api, "_op_session_id", 0)
        self.enabled = bool(self.port and requires_upload_port(info))
        self.handoff = bool(expected_handoff or expects_serial_handoff(info))
        self._esp_native_candidate = str(info.get("platform", "")).casefold() == "espressif32"
        self._handoff_after = handoff_after
        self.confirm_after = handoff_after if self.handoff else confirm_after
        self.scan_interval = scan_interval
        self.clock = clock or time.monotonic
        if scan is None:
            from serial.tools.list_ports import comports
            scan = comports
        self.scan = scan
        self.last_scan = float("-inf")
        self.missing_since = None
        self.identity = None
        if self.enabled and process.poll() is None:
            api._operation_serial_guard = self
            for row in getattr(api, "_last_known_ports", ()):
                if self._device(row) == self.port.casefold():
                    self.identity = self._identity(row)
                    self._observe_native_usb(row)
                    break
            # Capture USB identity before a declared reset can change its name.
            self._presence()

    def _owns_process(self) -> bool:
        return (getattr(self.api, "_op_session_id", 0) == self.session
                and getattr(self.api, "_active_process", None) is self.process)

    @staticmethod
    def _device(row) -> str:
        value = row.get("device", "") if isinstance(row, dict) else getattr(row, "device", "")
        return str(value or "").casefold()

    @staticmethod
    def _identity(row):
        if isinstance(row, dict):
            hwid = str(row.get("hwid", "") or "")
            number = re.search(r"\bSER=(\S+)", hwid)
            location = re.search(r"\bLOCATION=(\S+)", hwid)
            return ((number.group(1) if number else ""),
                    (location.group(1) if location else "")) if number or location else None
        number = str(getattr(row, "serial_number", "") or "")
        location = str(getattr(row, "location", "") or "")
        return (number, location) if number or location else None

    def _observe_native_usb(self, row) -> None:
        """Use observed native ESP USB silicon, never a guessed board model."""
        if not self._esp_native_candidate:
            return
        vid = row.get("vid") if isinstance(row, dict) else getattr(row, "vid", None)
        hwid = str(row.get("hwid", "") or "") if isinstance(row, dict) else ""
        if vid == 0x303A or re.search(r"\bVID:PID=303A:[0-9A-F]{4}\b", hwid, re.I):
            self.handoff = True
            self.confirm_after = self._handoff_after

    def _presence(self):
        try:
            rows = list(self.scan())
        except Exception:
            return None
        for row in rows:
            if self._device(row) == self.port.casefold():
                observed = self._identity(row)
                if (self.identity and observed and self.identity[0] and observed[0]
                        and self.identity[0] != observed[0]):
                    return False
                if self.identity is None:
                    self.identity = observed
                self._observe_native_usb(row)
                return True
        if self.handoff and self.identity:
            number, location = self.identity
            for row in rows:
                other = self._identity(row)
                if other and number and other[0]:
                    match = other[0] == number
                else:
                    match = bool(other and location and other[1] == location)
                if match:
                    return True
        return False

    def defer_selection_clear(self, port: str) -> bool:
        """The hotplug monitor must allow an explicitly expected USB handoff."""
        return bool(self.enabled and self.handoff and port == self.port
                    and self._owns_process() and self.process.poll() is None
                    and not getattr(self.api, "_operation_connection_loss", ""))

    def _terminate_and_reap(self) -> None:
        if not self._owns_process() or self.process.poll() is not None:
            return
        self.api._kill_active_process_tree(self.process)
        try:
            self.process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            # Popen targets the original process handle, never a recycled PID.
            self.process.kill()
            self.process.wait(timeout=3.0)

    def poll(self) -> None:
        if not self.enabled or not self._owns_process() or self.process.poll() is not None:
            return
        reason = str(getattr(self.api, "_operation_connection_loss", "") or "")
        if reason:
            self._terminate_and_reap()
            raise OperationConnectionLost(reason)
        now = self.clock()
        if now - self.last_scan < self.scan_interval:
            return
        self.last_scan = now
        present = self._presence()
        if present is not False:
            self.missing_since = None
            return
        if self.missing_since is None:
            self.missing_since = now
            return
        if now - self.missing_since < self.confirm_after:
            return
        reason = record_connection_loss(self.api, self.port)
        self._terminate_and_reap()
        raise OperationConnectionLost(reason)

    def wait(self) -> int:
        """Continue checking transport even after stdout closed unexpectedly."""
        while self.process.poll() is None:
            self.poll()
            try:
                self.process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                continue
        reason = str(getattr(self.api, "_operation_connection_loss", "") or "")
        if reason and getattr(self.api, "_op_session_id", 0) == self.session:
            raise OperationConnectionLost(reason)
        return self.process.poll()

    def finish(self) -> int:
        """Failure cleanup may unlock only after the captured child has exited."""
        warned = False
        while True:
            try:
                return self.wait()
            except (OperationConnectionLost, OSError, subprocess.TimeoutExpired):
                if self.process.poll() is not None:
                    return self.process.returncode
                if not (self._owns_process() and getattr(self.api, "_operation_connection_loss", "")):
                    raise
                # OS denial/delayed exit is not permission to abandon a writer.
                # Retain the worker and busy protection, with one diagnostic.
                if not warned:
                    warned = True
                    self.api.emit("console:log", {
                        "text": "The disconnected programmer is still stopping. The operation remains locked until its process exits.",
                        "tag": "warning", "newline": True,
                    })
                time.sleep(0.2)

    def close(self) -> None:
        if getattr(self.api, "_operation_serial_guard", None) is self:
            self.api._operation_serial_guard = None
