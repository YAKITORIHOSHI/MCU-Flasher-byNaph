#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations



from main.core.constants import *

class Theme:
    PALETTES = {
        "default": {
            "BG_DARKEST": "#0a0e14",
            "BG_DARK": "#10151c",
            "BG_MID": "#161d27",
            "BG_LIGHT": "#1c2532",
            "BG_HOVER": "#243040",
            "BORDER": "#2a3545",
            "BORDER_LIT": "#00d2ff",
            "TEXT": "#e0e6ed",
            "TEXT_DIM": "#8fa1b3",
            "TEXT_BRIGHT": "#ffffff",
            "CYAN": "#00d2ff",
            "CYAN_DIM": "#1f7872",
            "GREEN": "#5ccc6e",
            "GREEN_DIM": "#2d6636",
            "YELLOW": "#e8b83a",
            "YELLOW_DIM": "#7a6020",
            "RED": "#f05050",
            "RED_DIM": "#7a2828",
            "MAGENTA": "#c678dd",
            "PURPLE": "#b388ff",
            "PURPLE_DIM": "#9d7cc4",
            "BLUE": "#61afef",
            "ORANGE": "#d19a66",
            "BTN_COMPILE": "#2d7d46",
            "BTN_COMPILE_H": "#38a058",
            "BTN_UPLOAD": "#8244a0",
            "BTN_UPLOAD_H": "#a05cc0",
            "BTN_FULL": "#2077b0",
            "BTN_FULL_H": "#2899dd",
            "BTN_MONITOR": "#1a7a70",
            "BTN_MONITOR_H": "#22a090",
            "BTN_STOP": "#a03030",
            "BTN_STOP_H": "#cc4444",
            "BTN_CLEAR": "#3a4555",
            "BTN_CLEAR_H": "#4a5a70",
            "BTN_DIM": "#2a3342",
            "BTN_DIM_H": "#3a4555",
            "BTN_DANGER": "#7a2828",
            "BTN_DANGER_H": "#a03030",
        },
        "light": {
            "BG_DARKEST": "#f6f8fa",
            "BG_DARK": "#eef2f5",
            "BG_MID": "#ffffff",
            "BG_LIGHT": "#f0f3f6",
            "BG_HOVER": "#e1e4e8",
            "BORDER": "#d0d7de",
            "BORDER_LIT": "#0969da",
            "TEXT": "#24292f",
            "TEXT_DIM": "#57606a",
            "TEXT_BRIGHT": "#1f2328",
            "CYAN": "#0969da",
            "CYAN_DIM": "#0550ae",
            "GREEN": "#1a7f37",
            "GREEN_DIM": "#116329",
            "YELLOW": "#9a6700",
            "YELLOW_DIM": "#7d4e00",
            "RED": "#cf222e",
            "RED_DIM": "#a40e26",
            "MAGENTA": "#8250df",
            "PURPLE": "#6639ba",
            "PURPLE_DIM": "#542c9f",
            "BLUE": "#0969da",
            "ORANGE": "#bc4c00",
            "BTN_COMPILE": "#1f883d",
            "BTN_COMPILE_H": "#1a7f37",
            "BTN_UPLOAD": "#0969da",
            "BTN_UPLOAD_H": "#0858b8",
            "BTN_FULL": "#0969da",
            "BTN_FULL_H": "#0858b8",
            "BTN_MONITOR": "#0e8a7e",
            "BTN_MONITOR_H": "#0b7066",
            "BTN_STOP": "#cf222e",
            "BTN_STOP_H": "#b61c27",
            "BTN_CLEAR": "#eef1f4",
            "BTN_CLEAR_H": "#e1e4e8",
            "BTN_DIM": "#eef1f4",
            "BTN_DIM_H": "#e1e4e8",
            "BTN_DANGER": "#cf222e",
            "BTN_DANGER_H": "#b61c27",
        },
        "solarized_dark": {
            "BG_DARKEST": "#001b22",
            "BG_DARK": "#002b36",
            "BG_MID": "#073642",
            "BG_LIGHT": "#0d4a59",
            "BG_HOVER": "#115d70",
            "BORDER": "#166b80",
            "BORDER_LIT": "#2aa198",
            "TEXT": "#ffffff",
            "TEXT_DIM": "#d0e4e8",
            "TEXT_BRIGHT": "#ffffff",
            "CYAN": "#2aa198",
            "CYAN_DIM": "#208078",
            "GREEN": "#859900",
            "GREEN_DIM": "#586600",
            "YELLOW": "#b58900",
            "YELLOW_DIM": "#7a5d00",
            "RED": "#dc322f",
            "RED_DIM": "#93201e",
            "MAGENTA": "#d33682",
            "PURPLE": "#6c71c4",
            "PURPLE_DIM": "#4d5196",
            "BLUE": "#268bd2",
            "ORANGE": "#cb4b16",
            "BTN_COMPILE": "#586600",
            "BTN_COMPILE_H": "#859900",
            "BTN_UPLOAD": "#6c71c4",
            "BTN_UPLOAD_H": "#8389db",
            "BTN_FULL": "#268bd2",
            "BTN_FULL_H": "#3a9de0",
            "BTN_MONITOR": "#2aa198",
            "BTN_MONITOR_H": "#38b8ae",
            "BTN_STOP": "#dc322f",
            "BTN_STOP_H": "#e84a47",
            "BTN_CLEAR": "#0a4554",
            "BTN_CLEAR_H": "#115d70",
            "BTN_DIM": "#073642",
            "BTN_DIM_H": "#0d4a59",
            "BTN_DANGER": "#93201e",
            "BTN_DANGER_H": "#dc322f",
        }
    }

    # Initial default values
    BG_DARKEST  = "#0a0e14"
    BG_DARK     = "#10151c"
    BG_MID      = "#161d27"
    BG_LIGHT    = "#1c2532"
    BG_HOVER    = "#243040"
    BORDER      = "#2a3545"
    BORDER_LIT  = "#3d5068"

    TEXT        = "#c8d2dc"
    TEXT_DIM    = "#6b7d94"
    TEXT_BRIGHT = "#e8edf3"

    CYAN        = "#39c5bb"
    CYAN_DIM    = "#1f7872"
    GREEN       = "#5ccc6e"
    GREEN_DIM   = "#2d6636"
    YELLOW      = "#e8b83a"
    YELLOW_DIM  = "#7a6020"
    RED         = "#f05050"
    RED_DIM     = "#7a2828"
    MAGENTA     = "#c678dd"
    PURPLE      = "#b388ff"
    PURPLE_DIM  = "#9d7cc4"
    BLUE        = "#61afef"
    ORANGE      = "#d19a66"

    BTN_COMPILE   = "#2d7d46"
    BTN_COMPILE_H = "#38a058"
    BTN_UPLOAD    = "#8244a0"
    BTN_UPLOAD_H  = "#a05cc0"
    BTN_FULL      = "#2077b0"
    BTN_FULL_H    = "#2899dd"
    BTN_MONITOR   = "#1a7a70"
    BTN_MONITOR_H = "#22a090"
    BTN_STOP      = "#a03030"
    BTN_STOP_H    = "#cc4444"
    BTN_CLEAR     = "#3a4555"
    BTN_CLEAR_H   = "#4a5a70"
    BTN_DIM       = "#2a3342"
    BTN_DIM_H     = "#3a4555"
    BTN_DANGER    = "#7a2828"
    BTN_DANGER_H  = "#a03030"

    active_theme = "default"

    @classmethod
    def apply_theme(cls, mode: str = "default") -> str:
        mode_key = (mode or "default").lower().strip()
        if mode_key in ("solarized", "solarize", "solarized_dark", "solarize_dark"):
            mode_key = "solarized_dark"
        elif mode_key not in cls.PALETTES:
            mode_key = "default"
        palette = cls.PALETTES[mode_key]
        for key, val in palette.items():
            setattr(cls, key, val)
        cls.active_theme = mode_key
        return mode_key


__all__ = [
    "Theme"
]
