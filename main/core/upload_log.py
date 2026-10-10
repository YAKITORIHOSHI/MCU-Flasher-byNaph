"""Shared upload presentation; host runtimes and programmer commands stay separate."""
from __future__ import annotations

import re
from main.core.target_profile import serial_upload_speed, requires_upload_port


_ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_DIAGNOSTIC = re.compile(
    r"\b(?:fatal\s+error|error|warning|failed|failure|exception|timed?\s*out|"
    r"permission\s+denied|cannot\s+open|can't\s+open)\b", re.IGNORECASE,
)
_MEMORY = re.compile(
    r"(RAM|Flash):\s*\[[=\s]*\]\s*[\d.]+%\s+"
    r"\(used\s+\d+\s+bytes\s+from\s+\d+\s+bytes\)", re.IGNORECASE,
)


def _clean(line: str) -> str:
    return _ESCAPES.sub("", str(line)).strip()


def _bounded(value: object, size: int = 1024) -> str:
    return str(value).encode("utf-8")[:size].decode("utf-8", errors="ignore")


class UploadLog:
    """Format one upload stream without touching hardware or operation state."""

    def __init__(self, api, info: dict, board_name: str, port: str, upload_speed: str, *, backend="PlatformIO"):
        self.api = api
        self.info = dict(info or {})
        self.board_name = _bounded(board_name)
        self.port = _bounded(port) if requires_upload_port(self.info) else ""
        self.backend = backend
        self.is_esp = str(self.info.get("platform") or "").lower() in {"espressif32", "espressif8266"}
        self.upload_speed = serial_upload_speed(self.info, upload_speed)
        self._progress = api._new_upload_progress_state() if self.is_esp else None
        self._chip: dict[str, str] = {}
        self._chip_shown = False
        self._memory: dict[str, str] = {}
        self._memory_emitted: dict[str, str] = {}
        self._connection = ""
        self._phase = ""
        self._started = False
        self._finished = False
        self._verified = False
        self._verify_requested = False
        self._written_bytes = ""
        self._write_label = "Writing"
        self._read_label = "Verifying"
        self._bars: dict[str, tuple[float, str]] = {}

    def _emit(self, text: str, tag: str = "info", **details) -> None:
        self.api.emit("console:log", {"text": text, "tag": tag, "newline": True, **details})

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._emit("", "normal")
        self._emit("=" * 50, "header")
        self._emit(f"  ⬆  UPLOADING ({self.backend})", "header")
        self._emit("=" * 50, "header")
        fields = [("Board", self.board_name or "Selected target")]
        target = self.info.get("arduino_fqbn") if self.backend == "Arduino CLI" else ":".join(
            str(self.info.get(key) or "") for key in ("platform", "board"))
        if target and target != ":":
            fields.append(("Target", _bounded(target)))
        if self.port:
            fields.append(("Port", self.port))
        if self.info.get("upload_protocol"):
            fields.append(("Protocol", _bounded(self.info["upload_protocol"])))
        if self.upload_speed:
            origin = "selected" if self.is_esp else "board default"
            fields.append(("Upload Speed", f"{self.upload_speed} baud ({origin})"))
        elif self.port:
            fields.append(("Upload Speed", "Board recipe default"))
        else:
            fields.append(("Transport", "USB / programmer"))
        self.api._print_info_box("Upload Target", fields)
        self.api.emit("console:progress", {"action": "Uploading"})

    def _connection_row(self, status: str) -> None:
        if self._connection == status:
            return
        self._connection = status
        text, tag = {
            "connecting": ("  🔌 Connecting to bootloader…", "magenta"),
            "connected": (f"  ✔ Connected to {self.board_name or 'board'}", "success"),
            "failed": ("  ✖ Connection failed", "error"),
            "stopped": ("  ■ Connection stopped", "info"),
        }[status]
        key = f"{getattr(self.api, '_op_session_id', 0)}:upload-connection"
        self._emit(text, tag, progress_key=key)

    def _show_memory(self) -> None:
        for key in ("ram", "flash"):
            if key in self._memory and self._memory_emitted.get(key) != self._memory[key]:
                self._emit(f"  {self._memory[key]}")
                self._memory_emitted[key] = self._memory[key]

    def _show_chip(self) -> None:
        model = self._chip.get("Chip Model")
        if self._chip_shown or not model:
            return
        self._chip_shown = True
        self.api._print_chip_info_box(model, list(self._chip.items()))
        self._show_memory()

    def _phase_row(self, phase: str, text: str, tag: str = "info") -> None:
        if self._phase != phase:
            self._phase = phase
            self._emit(text, tag)

    def _bar(self, label: str, percent: float, detail: str = "") -> None:
        percent = min(100.0, max(0.0, percent))
        record = (percent, detail)
        if self._bars.get(label) == record:
            return
        self._bars[label] = record
        filled = int(percent * 24 / 100)
        bar = "▰" * filled + "▱" * (24 - filled)
        icon = "✔" if percent >= 100 else "⚙"
        suffix = f" | {detail}" if detail else ""
        self._emit(f"  {icon} {label:<14} [ {bar} ] {percent:5.1f}%{suffix}",
                   "success" if percent >= 100 else "info",
                   progress_key=f"{getattr(self.api, '_op_session_id', 0)}:upload-{label}")

    def _consume_programmer(self, clean: str) -> bool:
        """Format known AVR/BOSSA/DFU/OpenOCD records; leave other output intact."""
        low = clean.lower()
        if re.fullmatch(r"(?:avrdude:\s*)?(?:AVR device initialized and ready to accept instructions|Device signature = 0x[\da-f]+(?:\s+\([^\r\n]*\))?)", clean, re.I):
            self._connection_row("connected")
            return True
        rate = re.fullmatch(r"Overriding Baud Rate\s*:\s*(\d+)", clean, re.I)
        if rate and self.port and len(rate.group(1)) <= 10 and int(rate.group(1)) > 0:
            actual = rate.group(1)
            if actual != self.upload_speed:
                self._emit(f"  Upload baud reported by programmer: {actual}", "info")
            self.upload_speed = actual
            return True
        progress = re.fullmatch(r"(Reading|Writing)\s*\|\s*[# ]*\|\s*(\d+(?:\.\d+)?)%\s*([\d.]+s)?", clean, re.I)
        if progress:
            label = self._write_label if progress.group(1).lower() == "writing" else self._read_label if self._verify_requested else "Reading device"
            self._bar(label, float(progress.group(2)), progress.group(3) or "")
            return True
        write = re.fullmatch(r"avrdude:\s*writing (flash|eeprom)\s*\((\d+) bytes\):", clean, re.I)
        if write:
            self._phase = "writing"
            self._verify_requested = False
            self._write_label = "Writing EEPROM" if write.group(1).lower() == "eeprom" else "Writing"
            self._bar(self._write_label, 0, f"{write.group(2)} bytes → {write.group(1).lower()}")
            return True
        written = re.fullmatch(r"avrdude:\s*(\d+) bytes of (flash|eeprom) (written|verified)", clean, re.I)
        if written:
            verified = written.group(3).lower() == "verified"
            flash = written.group(2).lower() == "flash"
            if verified and flash:
                self._verified = True
            elif not verified and flash:
                self._written_bytes = written.group(1)
            label = ("Verifying" if verified else "Writing") + (" EEPROM" if not flash else "")
            self._bar(label, 100, f"{written.group(1)} bytes {written.group(3).lower()}")
            return True
        if re.fullmatch(r"avrdude:\s*verifying (?:flash|eeprom) memory against .+:", clean, re.I):
            self._verify_requested = True
            self._read_label = "Verifying EEPROM" if "eeprom" in low else "Verifying"
            self._phase_row("verifying", "  ◇ Verifying firmware against the board…")
            return True
        if re.fullmatch(r"avrdude:\s*(?:reading input file .+|load data (?:flash|eeprom) data from input file .+|input file .+ contains \d+ bytes|reading on-chip (?:flash|eeprom) data:|verifying \.{3}|safemode: Fuses OK \([^\r\n]+\)|avrdude done\.\s+Thank you\.)", clean, re.I):
            return True
        if re.fullmatch(r"avrdude done\.\s+Thank you\.", clean, re.I):
            return True
        # Native tools have different protocols; only recognizable stage records
        # become progress. Diagnostics are checked before reaching this method.
        generic = re.fullmatch(r"(Download|Downloading|Upload|Uploading|Writing|Programming|Verify|Verifying)\s*(?:\[[#=▰▱\s.>-]*\])?\s*(\d+(?:\.\d+)?)%\s*(.*)", clean, re.I)
        bare = re.fullmatch(r"\[[#=\s.>-]+\]\s*(\d+(?:\.\d+)?)%\s*(.*)", clean)
        if generic or (bare and self._phase in {"writing", "verifying"}):
            if generic:
                label = "Verifying" if generic.group(1).lower().startswith("verif") else "Writing"
                percent, detail = generic.group(2), generic.group(3)
            else:
                label = "Verifying" if self._phase == "verifying" else "Writing"
                percent, detail = bare.groups()
            self._phase = label.lower()
            self._bar(label, float(percent), _bounded(detail, 120))
            return True
        if re.fullmatch(r"\*\* (?:Programming Started|Verify Started|Programming Finished|Verified OK) \*\*", clean, re.I):
            if "verified ok" in low:
                self._verified = True
                self._bar("Verifying", 100)
            elif "finished" in low:
                self._bar("Writing", 100)
            elif "verify" in low:
                self._phase_row("verifying", "  ◇ Verifying firmware…")
            else:
                self._phase_row("writing", "  ⚙ Programming flash…")
            return True
        if re.fullmatch(r"(?:Write|Writing|Program|Programming|Verify|Verifying)(?: flash| memory)?(?:\.\.\.|:)?", clean, re.I):
            verifying = low.startswith("verif")
            self._phase_row("verifying" if verifying else "writing", f"  {'◇' if verifying else '⚙'} {clean}")
            return True
        return False

    def consume(self, line: str) -> bool:
        clean = _clean(line)
        if not clean or _DIAGNOSTIC.search(clean):
            return False
        memory = _MEMORY.fullmatch(clean)
        if memory:
            self._memory[memory.group(1).lower()] = _bounded(clean)
            if not self.is_esp:
                self._show_memory()
            return True
        if re.fullmatch(r"DEBUG:\s+Current\s+\([^)]+\)(?:\s+.*)?", clean, re.IGNORECASE):
            return True
        if re.fullmatch(r"AVAILABLE:\s+[\w.,+\- ]+", clean, re.IGNORECASE):
            return True
        protocol = re.fullmatch(r"CURRENT:\s+upload_protocol\s*=\s*([\w.+-]+)", clean, re.IGNORECASE)
        if protocol:
            if protocol.group(1) != self.info.get("upload_protocol"):
                self._emit(f"  Upload protocol: {protocol.group(1)}", "dim")
                self.info["upload_protocol"] = protocol.group(1)
            return True
        if clean in {"No dependencies", "Configuring upload protocol..."}:
            return True
        if self.port and clean in {f"Using manually specified: {self.port}", f"Serial port {self.port}"}:
            return True
        if re.fullmatch(r"Uploading .+\.(?:hex|bin|uf2|elf)", clean, re.I):
            self._phase_row("preparing", "  ◇ Firmware image ready; starting the programmer…")
            return True
        if not self.is_esp:
            return self._consume_programmer(clean)

        patterns = (
            ("Chip Model", r"Chip (?:is|type)\s*:?\s+(.+)"),
            ("Features", r"Features\s*:\s*(.+)"),
            ("Crystal", r"Crystal (?:is|frequency)\s*:?\s+(.+)"),
            ("MAC Address", r"MAC(?: address)?\s*:\s*(.+)"),
            ("Flash Size", r"(?:(?:Auto-detected|Detected)\s+)?Flash size\s*:\s*(.+)"),
        )
        for label, pattern in patterns:
            match = re.fullmatch(pattern, clean, re.IGNORECASE)
            if match:
                if not self._chip_shown:
                    self._chip[label] = _bounded(match.group(1))
                elif self._chip.get(label) != match.group(1):
                    # Some versions report detected flash size after starting
                    # the stub; retain later observations in the journal.
                    self._chip[label] = _bounded(match.group(1))
                    self._emit(f"  {label}: {self._chip[label]}")
                return True
        connected = re.fullmatch(
            r"Connected to (ESP[\w.-]*(?:\s+\([^()\r\n]*\))?)(?: on [^:\r\n]+)?:?",
            clean, re.IGNORECASE,
        )
        if connected:
            if not self._chip_shown and "Chip Model" not in self._chip:
                self._chip["Chip Model"] = _bounded(connected.group(1))
            self._connection_row("connected")
            return True
        if re.fullmatch(r"Connecting\.{1,}(?:_\.*)*", clean, re.IGNORECASE):
            self._connection_row("connecting")
            return True
        if re.fullmatch(r"(?:Uploading stub|Running stub|Stub running|Stub flasher running)\.{0,3}", clean, re.IGNORECASE):
            if self._chip.get("Chip Model"):
                self._connection_row("connected")
            self._show_chip()
            return True
        if re.fullmatch(r"esptool(?:\.py)? v\d+(?:\.\d+)+(?:-[\w.-]+)?", clean, re.IGNORECASE):
            return True
        if re.fullmatch(r"Changing baud rate to \d+\.{0,3}|Changed\.", clean, re.IGNORECASE):
            if clean.lower().startswith("changing"):
                self._emit(f"  ⚙ {clean}", "dim")
            return True
        if re.fullmatch(r"Configuring flash size\.{3}|SHA digest in image updated", clean, re.IGNORECASE):
            return True
        if re.fullmatch(r"Flash will be erased from 0x[0-9a-f]+ to 0x[0-9a-f]+\.{3}", clean, re.IGNORECASE):
            self._show_chip()
            self._phase_row("erasing", "  ⚡ Erasing selected flash regions…")
            return True
        legacy_progress = re.fullmatch(
            r"Writing at (0x[0-9a-f]+)\.{3}\s*\(\s*\d+(?:\.\d+)?\s*%\s*\)",
            clean, re.IGNORECASE,
        )
        if legacy_progress and not self._progress.get("stage_locked"):
            # Older esptool has no image-start row. Select its stage from the
            # actual address before using the unchanged Windows progress helper.
            self.api._select_upload_stage_for_address(self._progress, legacy_progress.group(1))
        if self.api._consume_esptool_upload_progress(
                self._progress, clean, before_progress=self._show_chip):
            self._phase = "writing"
            return True
        if re.fullmatch(r"Hash of data verified\.", clean, re.IGNORECASE):
            self._verified = True
            self._phase_row("verified", "  ✔ Flash data verified.", "success")
            return True
        if re.fullmatch(r"Hard resetting via (?:RTS|DTR) pin\.{0,3}", clean, re.IGNORECASE):
            self._phase_row("resetting", f"  ✔ {clean}", "success")
            return True
        if clean == "Leaving...":
            return True
        return False

    def finish(self, success: bool, duration: float, stopped: bool = False) -> None:
        if self._finished:
            return
        self._finished = True
        self._show_chip()
        self._show_memory()
        if stopped:
            if self._connection == "connecting":
                self._connection_row("stopped")
            self._emit("  ■ Upload stopped by user.", "warning")
            return
        if not success:
            if self._connection == "connecting":
                self._connection_row("failed")
            self._emit("  ✖ Upload failed.", "error")
            return
        self._emit("", "normal")
        board_label = f" {self.board_name}" if self.board_name else ""
        self._emit(f"  ✔ Upload successful!{board_label}", "success")
        fields: list[tuple[str, str]] = [("Board", self.board_name or "Selected target"),
                                       ("Uploader", self.backend),
                                       ("Result", "Written and verified" if self._verified else "Uploader completed successfully")]
        if self.port:
            speed = f" @ {self.upload_speed} baud" if self.upload_speed else ""
            fields.append(("Upload Port", self.port + speed))
        if self.info.get("upload_protocol"):
            fields.append(("Upload Protocol", _bounded(self.info["upload_protocol"])))
        if self._written_bytes:
            fields.append(("Firmware", f"{self._written_bytes} bytes"))
        fields.append(("Upload Time", f"{round(max(0.0, float(duration)), 2)}s"))
        self.api._print_info_box("Upload Summary", fields)
