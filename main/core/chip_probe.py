"""What the connected board says about itself, for the upload's Actual Board table.

ESP chips answer esptool (``MCUWebBackendAPI._probe_chip_info``). Every other board is read
here, as far as its connection allows:

* Classic AVR serial bootloaders are asked directly: Optiboot and other STK500v1 loaders
  (Uno, Nano, Pro Mini, Duemilanove) and the STK500v2 loader of the Mega. They report the
  chip's signature, which names the exact part and so its memories, and their own version.
* Any USB serial port names its bridge or native USB device (CH340, CP210x, FTDI, the Uno's
  ATmega16U2, a native-USB chip) and its serial number.
* The board's definition adds what no chip reports, such as its clock; those rows say so.

A probe only reads, gives up within a few seconds and never blocks the upload. When a
bootloader cannot be reached first (a native-USB board in another mode, a programmer), the
uploader's own records fill the table instead (``UploadLog``).
"""
from __future__ import annotations

import json
from pathlib import Path
import time

# Signature -> (part, flash bytes, SRAM bytes, EEPROM bytes), from the parts' datasheets.
AVR_PARTS: dict[str, tuple[str, int, int, int]] = {
    "1E950F": ("ATmega328P", 32768, 2048, 1024),
    "1E9514": ("ATmega328", 32768, 2048, 1024),
    "1E9516": ("ATmega328PB", 32768, 2048, 1024),
    "1E940B": ("ATmega168P", 16384, 1024, 512),
    "1E9406": ("ATmega168", 16384, 1024, 512),
    "1E9415": ("ATmega168PB", 16384, 1024, 512),
    "1E930A": ("ATmega88", 8192, 1024, 512),
    "1E930F": ("ATmega88P", 8192, 1024, 512),
    "1E9307": ("ATmega8", 8192, 1024, 512),
    "1E9502": ("ATmega32", 32768, 2048, 1024),
    "1E9403": ("ATmega16", 16384, 1024, 512),
    "1E9587": ("ATmega32U4", 32768, 2560, 1024),
    "1E9489": ("ATmega16U2", 16384, 512, 512),
    "1E9389": ("ATmega8U2", 8192, 512, 512),
    "1E9609": ("ATmega644", 65536, 4096, 2048),
    "1E960A": ("ATmega644P", 65536, 4096, 2048),
    "1E9705": ("ATmega1284P", 131072, 16384, 4096),
    "1E9706": ("ATmega1284", 131072, 16384, 4096),
    "1E9703": ("ATmega1280", 131072, 8192, 4096),
    "1E9704": ("ATmega1281", 131072, 8192, 4096),
    "1E9801": ("ATmega2560", 262144, 8192, 4096),
    "1E9802": ("ATmega2561", 262144, 8192, 4096),
    "1E9782": ("AT90USB1286", 131072, 8192, 4096),
    "1E9651": ("ATmega4809", 49152, 6144, 256),
    "1E9650": ("ATmega4808", 49152, 6144, 256),
    "1E930B": ("ATtiny85", 8192, 512, 512),
    "1E9206": ("ATtiny45", 4096, 256, 256),
    "1E930C": ("ATtiny84", 8192, 512, 512),
}

# USB VID:PID -> what answers on that port.
USB_DEVICES: dict[tuple[int, int], str] = {
    (0x2341, 0x0043): "ATmega16U2 (Arduino USB bridge)",
    (0x2341, 0x0001): "ATmega8U2 (Arduino USB bridge)",
    (0x2341, 0x0243): "ATmega16U2 (Arduino USB bridge)",
    (0x2A03, 0x0043): "ATmega16U2 (Arduino USB bridge)",
    (0x2341, 0x0010): "ATmega16U2 (Arduino Mega USB bridge)",
    (0x2341, 0x0042): "ATmega16U2 (Arduino Mega USB bridge)",
    (0x2A03, 0x0042): "ATmega16U2 (Arduino Mega USB bridge)",
    (0x2341, 0x8036): "ATmega32U4 native USB (Leonardo)",
    (0x2341, 0x0036): "ATmega32U4 bootloader (Leonardo)",
    (0x2341, 0x8037): "ATmega32U4 native USB (Micro)",
    (0x2341, 0x0037): "ATmega32U4 bootloader (Micro)",
    (0x2341, 0x804D): "SAMD21 native USB (Zero)",
    (0x2341, 0x004D): "SAMD21 bootloader (Zero)",
    (0x2341, 0x8057): "SAMD21 native USB (Nano 33 IoT)",
    (0x2341, 0x0069): "RA4M1 native USB (UNO R4 Minima)",
    (0x2341, 0x1002): "ESP32-S3 USB bridge (UNO R4 WiFi)",
    (0x1A86, 0x7523): "WCH CH340",
    (0x1A86, 0x7522): "WCH CH340K",
    (0x1A86, 0x5523): "WCH CH341",
    (0x1A86, 0x55D4): "WCH CH9102",
    (0x1A86, 0x55D3): "WCH CH343",
    (0x10C4, 0xEA60): "Silicon Labs CP210x",
    (0x10C4, 0xEA70): "Silicon Labs CP2105",
    (0x0403, 0x6001): "FTDI FT232R",
    (0x0403, 0x6015): "FTDI FT231X",
    (0x0403, 0x6010): "FTDI FT2232",
    (0x0403, 0x6014): "FTDI FT232H",
    (0x067B, 0x2303): "Prolific PL2303",
    (0x2E8A, 0x000A): "RP2040 native USB",
    (0x2E8A, 0x0003): "RP2040 boot ROM",
    (0x2E8A, 0x0009): "RP2350 native USB",
    (0x303A, 0x1001): "ESP32 USB Serial/JTAG",
    (0x303A, 0x0002): "ESP32-S2 native USB",
    (0x239A, 0x800B): "Adafruit native USB",
    (0x16C0, 0x0483): "Teensy native USB",
    (0x0483, 0x5740): "STM32 native USB",
    (0x0483, 0x374B): "ST-LINK/V2-1",
}


def _size(value: int) -> str:
    return f"{value // 1024} KB" if value >= 1024 and value % 1024 == 0 else (
        f"{value / 1024:.1f} KB" if value >= 1024 else f"{value} bytes")


def _clock(value) -> str:
    try:
        hertz = int(str(value).rstrip("Ll"))
    except (TypeError, ValueError):
        return ""
    return f"{hertz / 1_000_000:g} MHz" if hertz >= 1_000_000 else f"{hertz / 1000:g} kHz"


def board_manifest(info: dict) -> dict:
    """The board's PlatformIO definition, or nothing."""
    path = str((info or {}).get("pio_manifest") or "")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def avr_part(signature: str) -> tuple[str, int, int, int] | None:
    key = "".join(ch for ch in str(signature).upper().replace("0X", "") if ch in "0123456789ABCDEF")
    return AVR_PARTS.get(key[-6:]) if len(key) >= 6 else None


def usb_fields(port: str, *, comports=None) -> list[tuple[str, str]]:
    """The USB device behind a serial port: its bridge or native USB chip and serial number."""
    if not port:
        return []
    try:
        if comports is None:
            from serial.tools.list_ports import comports
        entries = list(comports())
    except Exception:
        return []
    entry = next((item for item in entries if getattr(item, "device", None) == port), None)
    if entry is None or getattr(entry, "vid", None) is None:
        return []
    vid, pid = int(entry.vid), int(entry.pid or 0)
    known = USB_DEVICES.get((vid, pid))
    described = " ".join(str(part) for part in (getattr(entry, "manufacturer", None),
                                                getattr(entry, "product", None)) if part)
    name = known or described or "USB device"
    fields = [("USB Bridge", f"{name} · {vid:04X}:{pid:04X}")]
    if known and described and described.lower() not in known.lower():
        fields.append(("USB Product", described[:80]))
    serial_number = str(getattr(entry, "serial_number", None) or "").strip()
    if serial_number:
        fields.append(("USB Serial", serial_number[:64]))
    return fields


def avr_fields(signature: str, info: dict, *, bootloader: str = "") -> tuple[list[tuple[str, str]], str]:
    """The table for a reported AVR signature, and a warning when it is not the selected chip."""
    hex_digits = "".join(ch for ch in str(signature).upper().replace("0X", "") if ch in "0123456789ABCDEF")[-6:]
    spaced = " ".join(hex_digits[index:index + 2] for index in range(0, len(hex_digits), 2))
    part = avr_part(hex_digits)
    manifest = board_manifest(info)
    upload = manifest.get("upload") or {}
    build = manifest.get("build") or {}
    if part is None:
        fields = [("Chip", "Unlisted AVR part"), ("Signature", spaced)]
    else:
        name, flash, sram, eeprom = part
        room = upload.get("maximum_size")
        flash_text = _size(flash) + (f" ({_size(int(room))} for sketches)"
                                     if isinstance(room, int) and 0 < room < flash else "")
        fields = [("Chip", name), ("Signature", spaced), ("Flash", flash_text),
                  ("SRAM", _size(sram)), ("EEPROM", _size(eeprom))]
    clock = _clock(build.get("f_cpu"))
    if clock:
        fields.append(("Clock", f"{clock} (board setting)"))
    if bootloader:
        fields.append(("Bootloader", bootloader))
    expected = str(info.get("mcu") or build.get("mcu") or "").lower()
    warning = ""
    if part is not None and expected and part[0].lower() != expected:
        warning = (f"Detected {part[0]}; the selected board is built for {expected}. "
                   "Select the matching board in Controls if the upload is refused.")
    return fields, warning


def definition_fields(info: dict) -> list[tuple[str, str]]:
    """What the board definition states, for chips no bootloader here can be asked."""
    manifest = board_manifest(info)
    build = manifest.get("build") or {}
    upload = manifest.get("upload") or {}
    mcu = str(info.get("mcu") or build.get("mcu") or "").strip()
    fields = []
    if mcu:
        fields.append(("Chip", f"{mcu.upper() if len(mcu) <= 8 else mcu} (board definition)"))
    clock = _clock(build.get("f_cpu"))
    if clock:
        fields.append(("Clock", f"{clock} (board definition)"))
    for label, key in (("Flash", "maximum_size"), ("RAM", "maximum_ram_size")):
        value = upload.get(key)
        if isinstance(value, int) and value > 0:
            fields.append((label, f"{_size(value)} (board definition)"))
    return fields


class _Port:
    """A serial port opened for a probe; nothing on it is written but bootloader queries."""

    def __init__(self, port: str, baud: int, serial_factory=None):
        if serial_factory is None:
            import serial
            serial_factory = serial.Serial
        options = {"baudrate": baud, "timeout": 0.05, "write_timeout": 1.0}
        try:
            self.serial = serial_factory(port, exclusive=True, **options)
        except TypeError:
            self.serial = serial_factory(port, **options)

    def reset(self) -> None:
        """The auto-reset every Arduino serial bootloader expects: DTR/RTS low, then high."""
        self.serial.dtr = False
        self.serial.rts = False
        time.sleep(0.25)
        self.serial.dtr = True
        self.serial.rts = True
        time.sleep(0.05)
        self.serial.reset_input_buffer()

    def read(self, count: int, timeout: float) -> bytes:
        data = b""
        deadline = time.monotonic() + timeout
        while len(data) < count and time.monotonic() < deadline:
            data += self.serial.read(count - len(data))
        return data

    def close(self) -> None:
        try:
            self.serial.close()
        except Exception:
            pass


# STK500v1, as Optiboot and ATmegaBOOT speak it.
_INSYNC, _OK, _EOP = 0x14, 0x10, 0x20


def _stk500v1(link: _Port, deadline: float) -> tuple[str, str] | None:
    def ask(command: bytes, reply: int) -> bytes | None:
        link.serial.write(command + bytes([_EOP]))
        answer = link.read(reply + 2, 0.3)
        return answer[1:-1] if len(answer) == reply + 2 and answer[0] == _INSYNC and answer[-1] == _OK else None

    while time.monotonic() < deadline:
        link.serial.write(bytes([0x30, _EOP]))  # STK_GET_SYNC
        if link.read(2, 0.2) == bytes([_INSYNC, _OK]):
            break
        link.serial.reset_input_buffer()
    else:
        return None
    link.serial.reset_input_buffer()
    major = ask(bytes([0x41, 0x81]), 1)  # STK_GET_PARAMETER, software major
    minor = ask(bytes([0x41, 0x82]), 1)
    ask(bytes([0x50]), 0)  # STK_ENTER_PROGMODE: a no-op for Optiboot, needed by older loaders
    signature = ask(bytes([0x75]), 3)  # STK_READ_SIGN
    ask(bytes([0x51]), 0)  # STK_LEAVE_PROGMODE: the sketch starts again
    if not signature:
        return None
    version = f"STK500v1, version {major[0]}.{minor[0]}" if major and minor else "STK500v1"
    return signature.hex().upper(), version


def _stk500v2(link: _Port, deadline: float) -> tuple[str, str] | None:
    sequence = [0]

    def ask(body: bytes) -> bytes | None:
        sequence[0] = (sequence[0] + 1) & 0xFF
        frame = bytes([0x1B, sequence[0], len(body) >> 8, len(body) & 0xFF, 0x0E]) + body
        checksum = 0
        for byte in frame:
            checksum ^= byte
        link.serial.write(frame + bytes([checksum]))
        head = link.read(5, 0.3)
        if len(head) != 5 or head[0] != 0x1B or head[4] != 0x0E:
            return None
        size = (head[2] << 8) | head[3]
        rest = link.read(size + 1, 0.3)
        if len(rest) != size + 1:
            return None
        check = 0
        for byte in head + rest:
            check ^= byte
        return rest[:-1] if check == 0 and rest[0] == body[0] else None

    signed = None
    while time.monotonic() < deadline and signed is None:
        signed = ask(bytes([0x01]))  # CMD_SIGN_ON
        if signed is None:
            link.serial.reset_input_buffer()
    if not signed or signed[1] != 0x00:
        return None
    major = ask(bytes([0x03, 0x91]))  # CMD_GET_PARAMETER, PARAM_SW_MAJOR
    minor = ask(bytes([0x03, 0x92]))
    parts = []
    for address in range(3):
        answer = ask(bytes([0x1B, 0x04, 0x30, 0x00, address, 0x00]))  # CMD_READ_SIGNATURE_ISP
        if not answer or len(answer) < 3 or answer[1] != 0x00:
            return None
        parts.append(answer[2])
    ask(bytes([0x11, 0x01, 0x01]))  # CMD_LEAVE_PROGMODE_ISP
    name = bytes(signed[3:3 + signed[2]]).decode("ascii", "replace") if len(signed) > 3 else "STK500v2"
    version = (f"{name} (STK500v2), version {major[2]}.{minor[2]}"
               if major and minor and len(major) > 2 and len(minor) > 2 else f"{name} (STK500v2)")
    return bytes(parts).hex().upper(), version


def probe_avr_bootloader(port: str, info: dict, *, serial_factory=None, budget: float = 2.5):
    """Ask an AVR serial bootloader for its chip's signature: (signature, bootloader) or None."""
    protocol = str((info or {}).get("upload_protocol") or "").lower()
    speaker = {"arduino": _stk500v1, "stk500v1": _stk500v1, "stk500": _stk500v1,
               "wiring": _stk500v2, "stk500v2": _stk500v2}.get(protocol)
    if speaker is None or not port:
        return None
    try:
        baud = int((info or {}).get("upload_speed") or (board_manifest(info).get("upload") or {}).get("speed") or 115200)
    except (TypeError, ValueError):
        baud = 115200
    link = None
    try:
        link = _Port(port, baud, serial_factory)
        link.reset()
        return speaker(link, time.monotonic() + budget)
    except Exception:
        return None
    finally:
        if link is not None:
            link.close()


def board_report(port: str, info: dict, *, serial_factory=None, comports=None):
    """(title, fields, warning) for a non-ESP board.

    A title of None means an AVR bootloader did not answer yet: avrdude reports the
    signature once it connects, and ``fields`` then holds only the USB rows to add to it.
    """
    info = dict(info or {})
    usb = usb_fields(port, comports=comports)
    if str(info.get("platform") or "").lower() == "atmelavr":
        probed = probe_avr_bootloader(port, info, serial_factory=serial_factory)
        if probed:
            fields, warning = avr_fields(probed[0], info, bootloader=probed[1])
            return "Actual Board", fields + usb, warning
        if port:
            return None, usb, ""
    fields = definition_fields(info) + usb
    return ("Connected Board" if usb else "Board Specs"), fields, ""
