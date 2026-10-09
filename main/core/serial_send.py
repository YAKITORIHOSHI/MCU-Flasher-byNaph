"""Bounded, single-attempt serial sends outside the GUI thread."""
from __future__ import annotations

from collections import deque
import threading


class SerialSendQueue:
    MAX_MESSAGE_BYTES = 64 * 1024
    MAX_QUEUED_BYTES = 256 * 1024
    MAX_MESSAGES = 16

    def __init__(self, api):
        self.api = api
        self.condition = threading.Condition()
        self.pending = deque()
        self.queued_bytes = 0
        self.active = None
        self.thread = None
        self.stopped = False

    def _log(self, text, *, generation=None, tag="warning"):
        self.api.emit("serial:log", {
            "text": text, "tag": tag, "newline": True, "stream": False,
            "generation": getattr(self.api, "_serial_generation", 0) if generation is None else generation,
        })

    def submit(self, text: str, line_ending="both") -> bool:
        endings = {"both": "\r\n", "nl": "\n", "cr": "\r", "none": ""}
        # Bound the allocation before UTF-8 expansion, then bound actual bytes.
        if len(text) > self.MAX_MESSAGE_BYTES:
            self._log("Cannot send: the command exceeds the serial send limit.", tag="error")
            return False
        data = (text + endings.get(line_ending, "\r\n")).encode("utf-8", errors="replace")
        if len(data) > self.MAX_MESSAGE_BYTES:
            self._log("Cannot send: the command exceeds the serial send limit.", tag="error")
            return False
        # A driver may be opening/closing elsewhere. Never wait on that lock in
        # a GUI callback; the user's input remains available for another Send.
        if not self.api._serial_lock.acquire(blocking=False):
            self._log("Cannot send while the serial connection is changing. Try Send again.")
            return False
        try:
            conn = self.api._serial_conn
            if (conn is None or not conn.is_open
                    or (getattr(self.api, "is_busy", False)
                        and getattr(self.api, "active_operation", None) != "compile")):
                self._log("Cannot send: Serial port not connected or reserved by an operation.", tag="error")
                return False
            request = (self.api._serial_generation, conn, self.api.current_port,
                       getattr(self.api, "current_baud", None), str(text), data)
        finally:
            self.api._serial_lock.release()
        with self.condition:
            if self.stopped:
                return False
            if (len(self.pending) + bool(self.active) >= self.MAX_MESSAGES
                    or self.queued_bytes + len(data) > self.MAX_QUEUED_BYTES):
                self._log("Cannot send: the serial send queue is full. Wait for the current send to finish.")
                return False
            self.pending.append(request)
            self.queued_bytes += len(data)
            if self.thread is None:
                try:
                    self.thread = threading.Thread(target=self._worker, name="MCU_SerialSend", daemon=True)
                    self.thread.start()
                except Exception as exc:
                    self.thread = None
                    self.pending.pop()
                    self.queued_bytes -= len(data)
                    self._log(f"Cannot start serial Send: {exc}", tag="error")
                    return False
            self.condition.notify()
        return True

    def _matches(self, request) -> bool:
        generation, conn, port, baud, _text, _data = request
        return (not self.stopped and self.api._serial_generation == generation
                and self.api._serial_conn is conn and self.api.current_port == port
                and getattr(self.api, "current_baud", None) == baud and conn.is_open
                and (not getattr(self.api, "is_busy", False)
                     or getattr(self.api, "active_operation", None) == "compile"))

    def discard(self, generation=None, conn=None) -> None:
        """Drop unsent requests; an already-started write is never repeated."""
        with self.condition:
            retained = deque()
            for request in self.pending:
                if ((generation is None or request[0] == generation)
                        and (conn is None or request[1] is conn)):
                    self.queued_bytes -= len(request[5])
                else:
                    retained.append(request)
            self.pending = retained

    def stop(self) -> None:
        with self.condition:
            self.stopped = True
            self.queued_bytes -= sum(len(request[5]) for request in self.pending)
            self.pending.clear()
            self.condition.notify_all()

    def _failed(self, request, error) -> None:
        generation, conn, port, _baud, _text, _data = request
        self.discard(generation, conn)
        disconnect = False
        with self.api._serial_lock:
            if self.api._serial_generation == generation and self.api._serial_conn is conn:
                finish = getattr(self.api, "_serial_log_finalize", None)
                if finish:
                    finish()
                self.api._serial_log_finalize = self.api._serial_log_flush = None
                self.api._serial_generation += 1
                self.api._serial_conn = None
                self.api.serial_running = False
                generation = self.api._serial_generation
                disconnect = True
                self.api.emit("serial:status", {
                    "generation": generation, "connected": False,
                    "state": "disconnected", "port": port,
                    "baud": getattr(self.api, "current_baud", None),
                })
        # Driver close targets only the captured connection outside the state
        # lock. A stale send failure never closes a replacement connection.
        try:
            conn.close()
        except Exception:
            pass
        if disconnect:
            self._log(f"Send error: {error}. Delivery may be incomplete; queued commands were discarded. Nothing was retried.",
                      generation=generation, tag="error")

    def _worker(self) -> None:
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.stopped or self.pending)
                if self.stopped:
                    return
                request = self.pending.popleft()
                self.active = request
            generation, conn, _port, _baud, text, data = request
            try:
                with self.api._serial_lock:
                    matches = self._matches(request)
                if not matches:
                    continue
                # The connection has a finite write_timeout. Do not flush:
                # serial drivers can wait indefinitely for a removed device.
                written = conn.write(data)
                if written != len(data):
                    raise OSError(f"only {written or 0} of {len(data)} bytes were accepted")
                with self.api._serial_lock:
                    matches = self._matches(request)
                if matches:
                    self._log(f"❯ {text}", generation=generation, tag="dim")
            except Exception as exc:
                self._failed(request, exc)
            finally:
                with self.condition:
                    self.queued_bytes -= len(data)
                    self.active = None
                    self.condition.notify_all()
