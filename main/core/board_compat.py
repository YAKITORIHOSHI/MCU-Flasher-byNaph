#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import re
from pathlib import Path


from main.core.constants import *
from main.core.theme import *
from main.core.config import *
from main.core.file_utils import *
from main.core.toolchain import *
from main.core.board_catalog import *

def find_board_for_platform(platform: str, variant_hint: str = "") -> str | None:
    """Return the safest resolved board for a platform / MCU-family hint.

    A chip probe such as ``ESP32-S3 (QFN56)`` identifies the silicon family,
    not the vendor PCB.  Many downloaded boards therefore share the exact same
    ``build.mcu`` value.  Never break that tie by display-name length (which
    previously made a short vendor name such as ``Bee S3`` win).  Instead,
    prefer the Arduino core's generic family definition when one exists -- a
    record whose Arduino board ID or variant equals its MCU family.  If the
    family remains genuinely ambiguous, return ``None`` rather than inventing a
    specific vendor board.
    """
    platform_norm = str(platform or "").lower().strip()
    hint_norm = _normalize_board_identity(variant_hint)
    rows: list[dict] = []

    for name, info in SUPPORTED_BOARDS.items():
        if not info.get("pio_resolved", True):
            continue
        if str(info.get("platform") or "").lower() != platform_norm:
            continue

        mcu_norm = _normalize_board_identity(info.get("mcu"))
        board_norm = _normalize_board_identity(info.get("board"))
        arduino_id_norm = _normalize_board_identity(info.get("arduino_board_id"))
        variant_norm = _normalize_board_identity(info.get("arduino_variant"))
        pio_name_norm = _normalize_board_identity(info.get("pio_name"))
        display_norm = _normalize_board_identity(name)

        if hint_norm:
            fields = (mcu_norm, board_norm, arduino_id_norm, variant_norm, pio_name_norm, display_norm)
            family_match = 0
            if mcu_norm and mcu_norm == hint_norm:
                family_match = 500
            elif any(value == hint_norm for value in fields if value):
                family_match = 420
            elif mcu_norm and (hint_norm in mcu_norm or mcu_norm in hint_norm):
                family_match = 340
            elif any(hint_norm in value or value in hint_norm for value in fields if value):
                family_match = 220
            if family_match <= 0:
                continue
        else:
            family_match = 0

        # Generic-family score is derived from metadata, not a board-name list.
        # Arduino's generic definitions normally use the MCU family itself as
        # the boards.txt key and/or build.variant (esp32s3 -> esp32s3, etc.).
        generic_score = 0
        if mcu_norm:
            if arduino_id_norm == mcu_norm:
                generic_score += 600
            if variant_norm == mcu_norm:
                generic_score += 520
            if board_norm == mcu_norm:
                generic_score += 260
        generic_words = f"{name} {info.get('pio_name','')}".lower()
        if "dev module" in generic_words or "development module" in generic_words:
            generic_score += 90
        if "devkit" in generic_words:
            generic_score += 40

        rows.append({
            "name": name,
            "family_match": family_match,
            "generic_score": generic_score,
            "mcu_norm": mcu_norm,
        })

    if not rows:
        return None

    if hint_norm:
        # If the hint exactly identifies an MCU family, restrict selection to
        # that family before considering genericness.
        exact_mcu = [row for row in rows if row["mcu_norm"] == hint_norm]
        if exact_mcu:
            rows = exact_mcu

    rows.sort(key=lambda row: (-row["family_match"], -row["generic_score"], row["name"].lower()))
    best = rows[0]

    # A silicon-only detection must not manufacture a vendor-specific board.
    # When multiple boards share the same exact MCU and none is identifiable as
    # the generic family definition, leave the user's selection unchanged.
    same_family = [row for row in rows if row["mcu_norm"] and row["mcu_norm"] == best["mcu_norm"]]
    if hint_norm and len(same_family) > 1 and best["generic_score"] <= 0:
        return None

    return str(best["name"])


def find_arduino_uno_board() -> str | None:
    """Return the installed Arduino Uno display name, if available."""
    for name, info in SUPPORTED_BOARDS.items():
        if info.get("platform") == "atmelavr" and name.strip().lower() in (
            "arduino uno", "arduino/genuino uno", "uno"
        ):
            return name
    for name, info in SUPPORTED_BOARDS.items():
        if info.get("platform") == "atmelavr" and str(info.get("board", "")).lower() == "uno":
            return name
    return find_board_for_platform("atmelavr")


def is_s3_board(p_board: str) -> bool:
    """True when *p_board* (a PlatformIO board id, e.g. from
    board_info["board"]) identifies an ESP32-S3 variant.

    Native-USB CDC build flags need to apply to whichever resolved board
    targets an ESP32-S3 family MCU, not to one specific hardcoded board ID.
    The canonical PlatformIO IDs discovered at runtime include the MCU family
    token, so this helper remains independent of any particular vendor board.
    """
    return "s3" in (p_board or "").lower()


def normalized_board_memory_options(board_info: dict | None) -> tuple[str | None, bool]:
    """Return ``(PlatformIO flash size, has_psram)`` for either board schema.

    Built-in entries historically used ``flash_size``/``psram`` while the
    downloaded boards.txt loader exposes ``flash_mb``/``has_psram``.  Keeping
    normalization in one place prevents main builds and reset builds from
    silently disagreeing about the same physical board.
    """
    info = dict(board_info or {})
    flash_size = info.get("flash_size")
    if not flash_size and info.get("flash_mb") is not None:
        try:
            flash_size = f"{float(info['flash_mb']):g}MB"
        except (TypeError, ValueError):
            flash_size = None
    has_psram = bool(info.get("psram") or info.get("has_psram"))
    return (str(flash_size) if flash_size else None), has_psram


def normalized_board_memory_type(board_info: dict | None) -> str:
    """Return an explicit Arduino memory type, rejecting unresolved templates."""
    value = str(dict(board_info or {}).get("memory_type") or "").strip().lower()
    return value if re.fullmatch(r"[a-z0-9_]+", value) else ""


def normalized_board_flash_mode(board_info: dict | None) -> str:
    """Return an explicit flash mode only when the board declares one."""
    value = str(dict(board_info or {}).get("flash_mode") or "").strip().lower()
    return value if value in {"dio", "dout", "qio", "qout"} else ""


def boards_by_platform(board_names, platforms: set[str]) -> set[str]:
    """Return the subset of *board_names* whose SUPPORTED_BOARDS platform
    is in *platforms*.

    Several rules need "every currently-known board belonging to platform
    X" (e.g. "exclude every ESP32-family board" when an AVR-exclusive
    header is found). The old code spelled that out as a fixed set of
    specific display names -- {"Arduino Uno", "ESP32 Dev Module",
    "ESP32-S3 Dev Module"} -- which only worked because those three
    names were hardcoded and therefore always exactly the boards that
    existed. Now that board entries are disk-discovered, there could be
    zero ESP32 boards downloaded, or several differently-named ones (a
    plain ESP32 dev board AND a separately-named S3 board, say) -- a
    fixed three-name list can't track either case. This looks up each
    name's actual platform in SUPPORTED_BOARDS and filters by that,
    so a rule means what it says ("every espressif32-platform board")
    regardless of how many such boards exist or what they're named.

    Board names not present in SUPPORTED_BOARDS are silently skipped
    rather than raising, since callers pass in sets that may already be
    a subset of SUPPORTED_BOARDS.keys() (e.g. mid-filter `boards`).
    """
    return {
        name for name in board_names
        if SUPPORTED_BOARDS.get(name, {}).get("platform") in platforms
    }

_ESPTOOL_CHIP_TO_PLATFORM_HINT: dict[str, tuple[str, str]] = {
    "ESP32-S3":   ("espressif32", "esp32s3"),
    "ESP32-S2":   ("espressif32", "esp32s2"),
    "ESP32-C3":   ("espressif32", "esp32c3"),
    "ESP32-C6":   ("espressif32", "esp32c6"),
    "ESP32-H2":   ("espressif32", "esp32h2"),
    "ESP32":      ("espressif32", "esp32"),
    "ESP8266EX":  ("espressif8266", ""),
    "ESP8266":    ("espressif8266", ""),
}


def detect_chip_on_port(port: str) -> tuple[str | None, str | None]:
    """Probe *port* with esptool and return (chip_name, board_display_name).

    Both values are None when detection fails (no ESP chip, port busy, etc.).
    The function must never raise — it is called from UI threads.

    Uses the esptool Python API directly (same approach as
    `_probe_chip_info`) rather than scraping CLI stdout with regexes — the
    CLI's "Detecting chip type..." / "Chip is ..." text has changed across
    esptool versions and is fragile to parse. `esp.CHIP_NAME` is a stable,
    canonical string (e.g. "ESP32-S3", "ESP32-C3", "ESP32", "ESP8266").

    Returns
    -------
    chip_name   : raw chip string from esptool, e.g. "ESP32-C3"
    board_name  : matching SUPPORTED_BOARDS key resolved via
                  find_board_for_platform, e.g. "ESP32 Dev Module" --
                  or None if no board of the matching platform has been
                  downloaded yet (find_board_for_platform found nothing)
    """
    try:
        # pyrefly: ignore [missing-import]
        import esptool

        if not hasattr(esptool, "get_default_connected_device"):
            return None, None

        esp = esptool.get_default_connected_device(
            serial_list=[port],
            port=port,
            connect_attempts=2,
            initial_baud=115200,
        )
        try:
            chip_name = getattr(esp, "CHIP_NAME", None)
        finally:
            try:
                esp._port.close()
            except Exception:
                pass

        if not chip_name:
            return None, None

        chip_name_upper = chip_name.upper()
        board = None
        # Match the longest/most specific key first (e.g. "ESP32-S3"
        # before the bare "ESP32" entry would also match as a substring)
        # so a variant-specific board is preferred whenever one has been
        # downloaded, falling back to the nearest available espressif32
        # board only when no exact variant match exists.
        for chip_key in sorted(_ESPTOOL_CHIP_TO_PLATFORM_HINT, key=len, reverse=True):
            if chip_key in chip_name_upper:
                platform, variant_hint = _ESPTOOL_CHIP_TO_PLATFORM_HINT[chip_key]
                board = find_board_for_platform(platform, variant_hint=variant_hint)
                break

        return chip_name, board
    except Exception:
        pass
    return None, None


def detect_board_compatibility(sketch_dir: Path) -> tuple[set[str], list[str]]:
    """Statically analyse source files and return which of the supported
    boards this sketch is likely compatible with.

    Returns
    -------
    compatible : set of board display-names (subset of SUPPORTED_BOARDS keys)
    reasons    : list of human-readable strings explaining each exclusion
                 (empty when nothing was excluded or detected)
    """
    all_texts: list[str] = []
    for ext in ("*.ino", "*.cpp", "*.c", "*.h"):
        for f in sorted(sketch_dir.glob(ext)):
            try:
                all_texts.append(f.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                pass
    if not all_texts:
        return set(SUPPORTED_BOARDS.keys()), []

    all_code = "\n".join(all_texts)

    # Collect normalised include names
    raw_includes = re.findall(r'#include\s*[<"]([^>"]+)[>"]', all_code, re.IGNORECASE)
    includes = {h.lower() for h in raw_includes}

    # Strip comments before scanning for API calls
    code_nc = re.sub(r'//.*?$', '', all_code, flags=re.MULTILINE)
    code_nc = re.sub(r'/\*.*?\*/', '', code_nc, flags=re.DOTALL)

    boards = set(SUPPORTED_BOARDS.keys())
    exclusions: list[str] = []

    # ── ESP8266-exclusive headers ──────────────────────────────────────────
    ESP8266_ONLY = {
        "esp8266wifi.h", "esp8266webserver.h", "esp8266httpclient.h",
        "esp8266mdns.h", "esp8266netbios.h", "esp8266ping.h",
        "esp8266wifimulti.h", "espsoftwareserial.h",
        "espconn.h", "user_interface.h",
    }
    hit = includes & ESP8266_ONLY
    if hit:
        boards = {b for b in boards if SUPPORTED_BOARDS.get(b, {}).get("platform") == "espressif8266"}
        exclusions.append(
            f"ESP8266-exclusive header(s) detected: {_fmt_hits(hit)} "
            f"→ compatible only with ESP8266 boards"
        )

    # ── ESP32-exclusive headers ────────────────────────────────────────────
    ESP32_ONLY_H = {
        "bledevice.h", "bleclient.h", "bleserver.h", "blescan.h",
        "blesecurity.h", "bleadvertising.h", "bleuuid.h",
        "nimbledevice.h",
        "nimblecharacteristic.h", "nimbleserver.h", "nimblescan.h",
        "nimbleclient.h", "nimblesecurity.h", "nimbleadvertising.h",
        "esp_bt.h", "esp_bt_main.h", "esp_gap_ble_api.h",
        "esp_gatts_api.h", "esp_gatt_common_api.h",
        "driver/ledc.h", "driver/mcpwm.h", "driver/pcnt.h",
        "driver/rmt.h", "driver/pulse_cnt.h",
        "soc/soc.h", "soc/rtc_cntl_reg.h",
        "esp_adc_cal.h", "esp_camera.h",
        "esp32servo.h", "fastaccelstepper.h",
        "wifiprov.h",
        "wifi_provisioning/manager.h",
        "wifi_provisioning/scheme_softap.h",
        "wifi_provisioning/scheme_ble.h",
        "wifi_provisioning/scheme_console.h",
    }
    hit = includes & ESP32_ONLY_H
    if hit:
        boards = {b for b in boards if SUPPORTED_BOARDS.get(b, {}).get("platform") == "espressif32"}
        exclusions.append(
            f"ESP32-exclusive header(s) detected: {_fmt_hits(hit)} "
            f"→ compatible only with ESP32 boards"
        )

    # ── ESP-family headers (ESP32 + ESP8266 only, rules out AVR) ──────────
    ESP_FAMILY = {
        "wifi.h", "wificlient.h", "wificlientsecure.h", "wifiserver.h",
        "wifiudp.h", "wifiap.h", "wifimulti.h", "wifiscan.h",
        "esp_wifi.h", "esp_event.h", "esp_log.h", "esp_system.h",
        "esp_sleep.h", "esp_partition.h", "esp_ota_ops.h",
        "nvs_flash.h", "nvs.h",
        "spiffs.h", "littlefs.h", "esp_spiffs.h", "esp_littlefs.h",
        "preferences.h", "update.h",
        "freertos/freertos.h", "freertos/task.h",
        "lwip/err.h", "lwip/sockets.h", "lwip/sys.h",
        "mbedtls/aes.h", "mbedtls/md.h",
    }
    hit = includes & ESP_FAMILY
    if hit:
        boards = {b for b in boards if SUPPORTED_BOARDS.get(b, {}).get("platform") in {"espressif32", "espressif8266"}}
        exclusions.append(
            f"ESP-family header(s) detected: {_fmt_hits(hit)} "
            f"→ not compatible with Arduino AVR (no WiFi/BT/NVS hardware)"
        )

    # ── ESP32-exclusive API calls ──────────────────────────────────────────
    ESP32_APIS = [
        (r'\bdacWrite\s*\(', "dacWrite()"),
        (r'\bledcSetup\s*\(', "ledcSetup()"),
        (r'\bledcAttachPin\s*\(', "ledcAttachPin()"),
        (r'\bledcWrite\s*\(', "ledcWrite()"),
        (r'\banalogReadMilliVolts\s*\(', "analogReadMilliVolts()"),
        (r'\bhallRead\s*\(', "hallRead()"),
        (r'\btouchRead\s*\(', "touchRead()"),
        (r'\besp_restart\s*\(', "esp_restart()"),
        (r'\bxTaskCreate\s*\(', "xTaskCreate()"),
        (r'\bxTaskCreatePinnedToCore\s*\(', "xTaskCreatePinnedToCore()"),
        (r'\bvTaskDelay\s*\(', "vTaskDelay()"),
        (r'\bpdMS_TO_TICKS\s*\(', "pdMS_TO_TICKS()"),
    ]
    api_hits = [label for pattern, label in ESP32_APIS if re.search(pattern, code_nc)]
    if api_hits:
        boards = {b for b in boards if SUPPORTED_BOARDS.get(b, {}).get("platform") == "espressif32"}
        preview = ", ".join(api_hits[:3]) + ("..." if len(api_hits) > 3 else "")
        exclusions.append(
            f"ESP32-exclusive API call(s): {preview} "
            f"→ compatible only with ESP32 boards"
        )

    # ── OTA updates need ≥ 4 MB flash ─────────────────────────────────────
    OTA_HDRS = {
        "update.h", "httpupdate.h", "esp_ota_ops.h", "esp_https_ota.h",
    }
    hit = includes & OTA_HDRS
    if hit:
        small_boards = [
            b for b in boards
            if SUPPORTED_BOARDS.get(b, {}).get("flash_mb")
            and SUPPORTED_BOARDS[b]["flash_mb"] < 4
        ]
        if small_boards:
            boards -= set(small_boards)
            exclusions.append(
                f"OTA update header(s) detected: {_fmt_hits(hit)} "
                f"→ needs ≥4 MB flash, excluded {len(small_boards)} board(s) "
                f"(e.g., {', '.join(sorted(small_boards)[:3])}...)"
            )

    # ── PSRAM usage needs a board with PSRAM populated by default ─────────
    PSRAM_RE = re.compile(
        r'\b(?:ps_malloc|esp_psram_new|esp_psram_calloc|esp_psram_realloc)\s*\('
        r'|\bESP\.getPsramSize\s*\('
        r'|\bheap_caps_malloc\s*\([^)]*MALLOC_CAP_SPIRAM'
        r'|\bheap_caps_realloc\s*\([^)]*MALLOC_CAP_SPIRAM'
    )
    psram_usage = ("esp_psram.h" in includes) or bool(PSRAM_RE.search(code_nc))
    if psram_usage:
        no_psram = [b for b in boards if not SUPPORTED_BOARDS.get(b, {}).get("has_psram")]
        if no_psram:
            exclusions.append(
                f"⚠ Sketch uses PSRAM (ps_malloc / esp_psram.h) → requires a "
                f"board variant with PSRAM populated; {len(no_psram)} board(s) "
                f"lack it (e.g., {', '.join(sorted(no_psram)[:3])}...) — "
                f"may fail at runtime"
            )

    # ── Serial1 / Serial2 rules out Uno ───────────────────────────────────
    if re.search(r'\bSerial[12]\b', code_nc):
        boards = {b for b in boards if SUPPORTED_BOARDS.get(b, {}).get("board") != "uno" and SUPPORTED_BOARDS.get(b, {}).get("platform") != "atmelavr"}
        exclusions.append(
            "Uses Serial1 / Serial2 — not compatible with Uno (which only has Serial)"
        )

    # ── AVR-exclusive headers (rules out both ESP boards) ─────────────────
    AVR_ONLY = {
        "avr/pgmspace.h", "avr/io.h", "avr/interrupt.h",
        "avr/wdt.h", "avr/eeprom.h",
    }
    hit = includes & AVR_ONLY
    if hit:
        boards = {b for b in boards if SUPPORTED_BOARDS.get(b, {}).get("platform") == "atmelavr"}
        exclusions.append(
            f"AVR-exclusive header(s) detected: {_fmt_hits(hit)} "
            f"→ compatible only with Arduino AVR"
        )

    # ── GPIO pin-number analysis ──────────────────────────────────────────
    gpio_result = _analyze_gpio_compatibility(sketch_dir)
    excluded_pins_summary = {}  # pins_str -> list of board names
    for board in gpio_result["excluded"]:
        if board in boards:
            boards.discard(board)
            bad = gpio_result["pin_hits"].get(board, [])
            bad_pins = sorted({pin for pin, _ in bad})
            if bad_pins:
                pin_list = ", ".join(str(p) for p in bad_pins[:6])
                if len(bad_pins) > 6:
                    pin_list += f" (+{len(bad_pins)-6} more)"
                excluded_pins_summary.setdefault(pin_list, []).append(board)

    for pins_str, board_names in excluded_pins_summary.items():
        if len(board_names) > 3:
            exclusions.append(
                f"GPIO pin(s) out of range ({pins_str}) for {len(board_names)} boards "
                f"(e.g., {', '.join(sorted(board_names)[:3])}...) → not compatible"
            )
        else:
            for board in board_names:
                exclusions.append(
                    f"GPIO pin(s) out of range for {board}: {pins_str} → not compatible"
                )

    # GPIO reserved-pin warnings (don't exclude, just caution)
    warnings_summary = {}  # (pin, ctx, msg_type) -> list of board names
    for board, pin, ctx, msg_type in gpio_result["warnings"]:
        if board in boards:
            warnings_summary.setdefault((pin, ctx, msg_type), []).append(board)

    for (pin, ctx, msg_type), board_names in warnings_summary.items():
        if len(board_names) > 3:
            exclusions.append(
                f"⚠ GPIO {pin} ({ctx}) is reserved for {msg_type} on {len(board_names)} boards "
                f"(e.g., {', '.join(sorted(board_names)[:3])}...) — may cause instability"
            )
        else:
            for board in board_names:
                exclusions.append(
                    f"⚠ GPIO {pin} ({ctx}) is reserved for {msg_type} on most {board.split()[0]} modules — may cause instability"
                )

    return boards, exclusions

def _fmt_hits(hit_set: set[str], max_show: int = 3) -> str:
    """Format a set of matched header names for display."""
    items = sorted(hit_set)
    shown = items[:max_show]
    rest  = len(items) - max_show
    result = ", ".join(shown)
    if rest > 0:
        result += f" (+{rest} more)"
    return result

def _board_family(name: str, platform: str | None = None) -> str:
    """Bucket a board display-name into a chip-family label for compact
    compatibility listings (e.g. 'ESP32-S3', 'ESP8266', 'Uno').

    ``platform`` (the espressif32 / espressif8266 / atmelavr field from the
    board metadata) is the authoritative fallback when the display name
    doesn't spell out the chip — e.g. 'AMYboard' or 'Adafruit FunHouse'."""
    upper = name.upper()
    if "ESP32-S3" in upper:
        return "ESP32-S3"
    if "ESP32-C6" in upper:
        return "ESP32-C6"
    if "ESP32-C3" in upper:
        return "ESP32-C3"
    if "ESP32-S2" in upper:
        return "ESP32-S2"
    if "ESP32-C2" in upper:
        return "ESP32-C2"
    if "ESP32" in upper:
        return "ESP32"
    # Boards like 'LOLIN S3 Mini', 'M5AtomS3', 'Bee S3' or 'CodeCell C3'
    # carry the chip suffix without the full 'ESP32-X' prefix.
    if "S3" in upper:
        return "ESP32-S3"
    if "C6" in upper:
        return "ESP32-C6"
    if "C3" in upper:
        return "ESP32-C3"
    if "S2" in upper:
        return "ESP32-S2"
    if "C2" in upper:
        return "ESP32-C2"
    if "ESP8266" in upper or "NODEMCU" in upper or "8266" in upper:
        return "ESP8266"
    if "UNO" in upper:
        return "Uno"
    # Names like 'Heltec WiFi LoRa 32', 'Node32s', 'LOLIN32' or 'Nano32'
    # never spell out 'ESP32' but are ESP32-based boards.
    if "32" in upper:
        return "ESP32"
    if platform == "espressif32":
        return "ESP32 (other)"
    if platform == "espressif8266":
        return "ESP8266 (other)"
    if platform == "atmelavr":
        return "Arduino AVR"
    return name

def _format_compat_label(boards: set[str]) -> str:
    """Turn the compatible board set into a display string dynamically."""
    if not boards:
        return "Unknown / Incompatible"

    ordered = sorted({
        _board_family(b, SUPPORTED_BOARDS.get(b, {}).get("platform")) for b in boards
    })
    if len(ordered) == 1:
        return ordered[0]
    if len(ordered) == 2:
        return f"{ordered[0]} and {ordered[1]}"
    return ", ".join(ordered[:-1]) + f", and {ordered[-1]}"

def _analyze_gpio_compatibility(sketch_dir: Path) -> dict:
    """Scan all source files for GPIO function calls and resolve pin numbers.

    Detects literal integers AND #define / const-int aliases.
    Returns:
        excluded : set of board names ruled out by out-of-range GPIO usage
        warnings : list of (board_name, message) for reserved-pin cautions
        pin_hits : dict board_name -> [(pin_num, context_str), ...]
    """
    GPIO_FUNCS = [
        "pinMode", "digitalWrite", "digitalRead",
        "analogWrite", "analogRead", "analogReadResolution",
        "touchRead", "dacWrite", "ledcAttachPin",
        "pulseIn", "pulseInLong",
        "tone", "noTone",
        "attachInterrupt", "detachInterrupt",
        "shiftIn", "shiftOut",
    ]

    all_texts: list[str] = []
    for ext in ("*.ino", "*.cpp", "*.c", "*.h"):
        for f in sorted(sketch_dir.glob(ext)):
            try:
                all_texts.append(f.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                pass
    if not all_texts:
        return {"excluded": set(), "warnings": [], "pin_hits": {}}

    all_code = "\n".join(all_texts)

    # Strip comments before scanning
    code_nc = re.sub(r'//.*?$', '', all_code, flags=re.MULTILINE)
    code_nc = re.sub(r'/\*.*?\*/', '', code_nc, flags=re.DOTALL)

    # Resolve #define NAME <int> and const int NAME = <int>
    defines: dict[str, int] = {}
    for m in re.finditer(r'#\s*define\s+(\w+)\s+(0x[0-9a-fA-F]+|\d+)\b', all_code):
        try:
            defines[m.group(1)] = int(m.group(2), 0)
        except ValueError:
            pass
    for m in re.finditer(
        r'\bconst\s+(?:int|uint8_t|uint16_t|byte)\s+(\w+)\s*=\s*(0x[0-9a-fA-F]+|\d+)\s*;',
        all_code
    ):
        try:
            defines[m.group(1)] = int(m.group(2), 0)
        except ValueError:
            pass

    # Extract pin literals from GPIO function calls
    func_pat = "|".join(re.escape(fn) for fn in GPIO_FUNCS)
    call_re  = re.compile(
        rf'\b({func_pat})\s*\(\s*([A-Za-z_]\w*|\d+)\s*[,)]',
        re.MULTILINE
    )
    pin_calls: list[tuple[int, str]] = []
    for m in call_re.finditer(code_nc):
        arg = m.group(2)
        pin = int(arg) if arg.isdigit() else defines.get(arg)
        if pin is not None:
            pin_calls.append((pin, f"{m.group(1)}({arg}…)"))

    # Classify each pin against board limits dynamically
    excluded: set[str] = set()
    warnings: list[tuple[str, str]] = []
    pin_hits: dict[str, list] = {name: [] for name in SUPPORTED_BOARDS.keys()}

    seen_reserved: set[tuple[str, int]] = set()   # avoid duplicate warnings

    for pin, ctx in pin_calls:
        for board_name, b_info in SUPPORTED_BOARDS.items():
            platform = b_info.get("platform", "")
            board_id = b_info.get("board", "").lower()
            
            # Determine maximum GPIO pin limits dynamically
            max_pin = 999
            if platform == "atmelavr":
                max_pin = 19
            elif platform == "espressif8266":
                max_pin = 16
            elif platform == "espressif32":
                if "s3" in board_id:
                    max_pin = 48
                elif "c3" in board_id:
                    max_pin = 21
                elif "c6" in board_id:
                    max_pin = 30
                elif "s2" in board_id:
                    max_pin = 46
                else:
                    max_pin = 39

            if pin > max_pin:
                pin_hits[board_name].append((pin, ctx))

            # Determine reserved flash pins
            reserved_pins = set()
            is_s3 = False
            if platform == "espressif32":
                if "s3" in board_id:
                    reserved_pins = {26, 27, 28, 29, 30, 31, 32}
                    is_s3 = True
                else:
                    reserved_pins = {6, 7, 8, 9, 10, 11}
            elif platform in ("espressif32", "espressif8266"):
                reserved_pins = {6, 7, 8, 9, 10, 11}

            if pin in reserved_pins:
                key = (board_name, pin)
                if key not in seen_reserved:
                    seen_reserved.add(key)
                    msg_type = "SPI flash/PSRAM" if is_s3 else "SPI flash"
                    warnings.append((board_name, pin, ctx, msg_type))

    for board, hits in pin_hits.items():
        if hits:
            excluded.add(board)

    return {"excluded": excluded, "warnings": warnings, "pin_hits": pin_hits}


__all__ = [
    "_analyze_gpio_compatibility",
    "_board_family",
    "_fmt_hits",
    "_format_compat_label",
    "boards_by_platform",
    "detect_board_compatibility",
    "detect_chip_on_port",
    "find_arduino_uno_board",
    "find_board_for_platform",
    "is_s3_board",
    "normalized_board_flash_mode",
    "normalized_board_memory_options",
    "normalized_board_memory_type"
]
