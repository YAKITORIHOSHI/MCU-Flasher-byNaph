"""Local SVG assets and semantic names, usable before either GUI toolkit loads."""
from functools import lru_cache
from pathlib import Path
import re

ASSET_ROOT = Path(__file__).resolve().parents[1] / "assets" / "icons" / "actions"
NAMES = frozenset(path.stem for path in ASSET_ROOT.glob("*.svg"))


@lru_cache(maxsize=96)
def svg_source(name, color="currentColor"):
    if name not in NAMES:
        raise ValueError(f"Unknown SVG icon: {name}")
    if color != "currentColor" and not re.fullmatch(r"#[a-fA-F0-9]{6}", str(color)):
        raise ValueError("SVG ink must be an RGB hex color")
    source = (ASSET_ROOT / (name + ".svg")).read_text(encoding="utf-8")
    if len(source) > 8192:
        raise ValueError("Action SVG exceeds its size limit")
    return source.replace("currentColor", str(color))


def action_name(text):
    """Only application action labels enter here; never rewrite user content."""
    label = str(text).strip().casefold()
    if label.startswith("copied"):
        return "check"
    if label.startswith("copy failed"):
        return "warning"
    labels = (
        ("copy", "copy"), ("pause", "pause"), ("resume", "resume"),
        ("clear", "clear"), ("kill", "clear"), ("delete", "clear"),
        ("cancel", "close"), ("close", "close"), ("add", "add"),
        ("create", "add"), ("rename", "modify"), ("modify", "modify"),
        ("hard reset", "erase"), ("soft reset", "reload"), ("reset", "reload"),
        ("reload", "reload"), ("refresh", "reload"), ("retry", "reload"),
        ("compile", "compile"), ("compiling", "compile"), ("building", "compile"),
        ("upload", "upload"), ("stop", "stop"), ("clean", "clean"),
        ("save", "save"), ("download", "download"), ("update", "upload"),
        ("open folder", "project"), ("browse", "project"), ("project", "project"),
        ("settings", "settings"), ("options", "settings"), ("filter", "filter"),
        ("search", "search"), ("check", "check"), ("run check", "resume"),
        ("archive", "archive"), ("folder", "project"), ("extracted", "project"), ("both", "package"),
        ("prepare", "repair"), ("view source", "file"), ("view examples", "book"),
        ("technical details", "details"), ("show technical", "details"),
        ("hide technical", "details"), ("power", "terminal"), ("command prompt", "terminal"),
    )
    if "ai assistant" in label or label in ("hide ai", "ai", "opencode ai assistant"):
        return "ai"
    for prefix, name in labels:
        if label.startswith(prefix):
            return name
    return ""


def status_name(tag):
    return {"success": "check", "ok": "check", "success_bold_lg": "check",
            "error": "error", "fail": "error", "severe_alert": "error",
            "warning": "warning", "warn": "warning", "header": "compile",
            "section": "setup", "subsection": "package", "system": "info",
            "info": "info", "update": "upload"}.get(str(tag), "")


def control_assets(mode):
    """Immutable, reviewed SVG inks for native stylesheet control states."""
    from main.core.theme import Theme, get_palette
    palette = get_palette(mode)
    key = next(name for name, colors in Theme.PALETTES.items()
               if colors["BG_DARKEST"] == palette["BG_DARKEST"])
    folder = ASSET_ROOT.parent / "controls"
    return {state: (folder / (key + "-" + state + ".svg")).as_posix()
            for state in ("checked", "hover", "disabled", "chevron", "setup-hover")}
