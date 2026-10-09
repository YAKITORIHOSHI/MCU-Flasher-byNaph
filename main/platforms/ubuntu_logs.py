"""Present native Ubuntu build/upload output without changing programmer work.

These presenters consume only recognized child-output records. They never open
ports, inspect package stores, start processes or change operation phases.
"""
from __future__ import annotations

import re


_ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_DIAGNOSTIC = re.compile(
    r"\b(?:fatal\s+error|error|warning|failed|failure|exception|timed?\s*out|"
    r"permission\s+denied|cannot\s+open|can't\s+open)\b", re.IGNORECASE,
)
_PACKAGE = re.compile(
    r"\s+-\s+((?:framework|toolchain|tool|platform)-[\w.+-]+)\s+@\s+"
    r"(\S+(?:\s+\([^()\r\n]+\))?)\s*", re.IGNORECASE,
)
_MEMORY = re.compile(
    r"(RAM|Flash):\s*\[[=\s]*\]\s*[\d.]+%\s+"
    r"\(used\s+\d+\s+bytes\s+from\s+\d+\s+bytes\)", re.IGNORECASE,
)


def _clean(line: str) -> str:
    return _ESCAPES.sub("", str(line)).strip()


def _bounded(value: object, size: int = 1024) -> str:
    return str(value).encode("utf-8")[:size].decode("utf-8", errors="ignore")


class UbuntuBuildInfo:
    """Collect the exact selected target and its actual PlatformIO metadata."""

    def __init__(self, api, board_info: dict, board_name: str):
        self.api = api
        self.info = dict(board_info or {})
        self.board_name = _bounded(board_name)
        self.platform = ""
        self.hardware = ""
        self.packages: list[str] = []
        self.package_bytes = 0
        self.packages_omitted = False
        self._in_packages = False
        self._have_metadata = False
        self._shown = False

    def consume(self, line: str) -> bool:
        clean = _clean(line)
        if not clean:
            return False
        if _DIAGNOSTIC.search(clean):
            if self._have_metadata:
                self.flush()
            self._in_packages = False
            return False
        for pattern, attribute in (
            (r"PLATFORM:\s+(.+\(\d[^()\r\n]*\)\s*>\s*\S.+)", "platform"),
            (r"HARDWARE:\s+(.+\b\d+(?:\.\d+)?\s*[kmg]?Hz\b.+(?:RAM|Flash)\b.*)", "hardware"),
        ):
            match = re.fullmatch(pattern, clean, re.IGNORECASE)
            if match:
                if not self._shown:
                    setattr(self, attribute, _bounded(match.group(1)))
                self._have_metadata = True
                self._in_packages = False
                return True
        if clean.upper() == "PACKAGES:":
            self._in_packages = self._have_metadata = True
            return True
        package = _PACKAGE.fullmatch(_ESCAPES.sub("", str(line)).rstrip("\r\n")) if self._in_packages else None
        if package:
            row = f"{package.group(1)} @ {package.group(2)}"
            if not self._shown and row not in self.packages:
                row_bytes = len(row.encode("utf-8")) + (2 if self.packages else 0)
                if len(self.packages) < 24 and self.package_bytes + row_bytes <= 4096:
                    self.packages.append(row)
                    self.package_bytes += row_bytes
                else:
                    self.packages_omitted = True
            return True
        self._in_packages = False
        # DEBUG normally sits between HARDWARE and PACKAGES. Leave its own
        # routing to the backend while collecting the complete standard block.
        if re.fullmatch(r"DEBUG:\s+Current\s+\([^)]+\)(?:\s+.*)?", clean, re.IGNORECASE):
            return False
        if self._have_metadata:
            self.flush()
        return False

    def flush(self) -> None:
        if self._shown:
            return
        self._shown = True
        fields: list[tuple[str, str]] = []
        if self.board_name:
            fields.append(("Board", self.board_name))
        platform = str(self.info.get("platform") or "").strip()
        board = str(self.info.get("board") or "").strip()
        if platform and board:
            fields.append(("Target", _bounded(f"{platform}:{board}")))
        if self.info.get("framework"):
            fields.append(("Framework", _bounded(self.info["framework"])))
        if self.platform:
            fields.append(("Platform", self.platform))
        if self.hardware:
            fields.append(("Hardware", self.hardware))
        if self.packages:
            fields.append(("Packages", "; ".join(self.packages)))
        if self.packages_omitted:
            fields.append(("Package details", "Additional package metadata omitted"))
        if fields:
            self.api._print_info_box("Board Information", fields)


class UbuntuUploadLog:
    """Apply Windows' console presentation to a single native uploader stream."""

    def __init__(self, api, info: dict, board_name: str, port: str, upload_speed: str):
        self.api = api
        self.info = dict(info or {})
        self.board_name = _bounded(board_name)
        self.port = _bounded(port)
        self.is_esp = str(self.info.get("platform") or "").lower() in {"espressif32", "espressif8266"}
        # The native builder overrides baud only for ESP serial targets. Other
        # boards retain their manifest defaults, regardless of the ESP control.
        speed = _bounded(upload_speed if self.is_esp else self.info.get("upload_speed") or "", 64).strip()
        self.upload_speed = speed if speed.isascii() and speed.isdigit() and int(speed) > 0 else ""
        self._progress = api._new_upload_progress_state() if self.is_esp else None
        self._chip: dict[str, str] = {}
        self._chip_shown = False
        self._memory: dict[str, str] = {}
        self._memory_emitted: dict[str, str] = {}
        self._connection = ""
        self._phase = ""
        self._started = False
        self._finished = False

    def _emit(self, text: str, tag: str = "info", **details) -> None:
        self.api.emit("console:log", {"text": text, "tag": tag, "newline": True, **details})

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._emit("", "normal")
        self._emit("=" * 50, "header")
        self._emit("  ⬆  UPLOADING (PlatformIO)", "header")
        self._emit("=" * 50, "header")
        board_label = f" | Board : {self.board_name}" if self.board_name else ""
        if self.port:
            speed_label = f" | Upload Speed : {self.upload_speed}" if self.upload_speed else ""
            self._emit(f"  Port : {self.port}{board_label}{speed_label}", "port_highlight")
        else:
            fields = [f"Board : {self.board_name}"] if self.board_name else []
            if self.info.get("upload_protocol"):
                fields.append(f"Upload Protocol : {_bounded(self.info['upload_protocol'])}")
            if fields:
                self._emit("  " + " | ".join(fields), "port_highlight")
        self.api.emit("console:progress", {"action": "Uploading"})

    def _connection_row(self, status: str) -> None:
        if self._connection == status:
            return
        self._connection = status
        text, tag = {
            "connecting": ("  🔌 Connecting to bootloader…", "magenta"),
            "connected": ("  ✔ Connected", "success"),
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
            self._emit(f"  Upload protocol: {protocol.group(1)}", "dim")
            return True
        if clean in {"No dependencies", "Configuring upload protocol..."}:
            return True
        if self.port and clean in {f"Using manually specified: {self.port}", f"Serial port {self.port}"}:
            return True
        if not self.is_esp:
            return False

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
        board_label = f" {self.board_name} is running…" if self.board_name else ""
        self._emit(f"  ✔ Upload successful!{board_label}", "success")
        fields: list[tuple[str, str]] = []
        if self.port:
            speed = f" @ {self.upload_speed} baud" if self.upload_speed else ""
            fields.append(("Upload Port", self.port + speed))
        elif self.info.get("upload_protocol"):
            fields.append(("Upload Protocol", _bounded(self.info["upload_protocol"])))
        fields.append(("Upload Time", f"{round(max(0.0, float(duration)), 2)}s"))
        self.api._print_info_box("Upload Summary", fields)
