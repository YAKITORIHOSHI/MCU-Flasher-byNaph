"""Describe Ubuntu serial entries using enumeration metadata, without opening ports."""
from __future__ import annotations

import re


def _detail(value) -> str:
    text = str(value or "").strip()
    if "".join(text.casefold().split()) in {"", "n/a", "na", "none", "null", "unknown",
                                           "notavailable", "notapplicable", "-"}:
        return ""
    return text


def port_entry(info) -> dict[str, str] | None:
    """Omit unidentified tty placeholders; retain genuine device descriptions/IDs."""
    device = _detail(getattr(info, "device", ""))
    if not device:
        return None
    description = " - ".join(part for value in str(getattr(info, "description", "") or "").split(" - ")
                             if (part := _detail(value)))
    # A repeated tty name or symlink destination identifies the node, not hardware.
    if description in (device, device.rsplit("/", 1)[-1]):
        description = ""
    hwid = _detail(re.sub(r"(?:^|\s)LINK=\S+", "", str(getattr(info, "hwid", "") or "")))
    if not description:
        details = [_detail(getattr(info, field, "")) for field in ("product", "interface", "manufacturer")]
        description = " - ".join(dict.fromkeys(item for item in details if item)) or hwid
    if not description:
        return None
    return {"device": device, "description": description, "hwid": hwid}
