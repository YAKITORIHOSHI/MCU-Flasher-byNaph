"""Toolkit-independent layout arithmetic; inputs stay in their native units."""
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkArea:
    x: int
    y: int
    width: int
    height: int


def fit_rect(x, y, width, height, area: WorkArea, margin=0):
    """Fit the complete rectangle, including negative monitor origins."""
    margin = max(0, min(margin, area.width // 4, area.height // 4))
    width = max(1, min(int(width), max(1, area.width - 2 * margin)))
    height = max(1, min(int(height), max(1, area.height - 2 * margin)))
    x = max(area.x + margin, min(int(x), area.x + area.width - margin - width))
    y = max(area.y + margin, min(int(y), area.y + area.height - margin - height))
    return x, y, width, height


def preferred_size(area: WorkArea, width, height, ratio=.88, clearance=(24, 48)):
    """Reserve room for window decoration without applying DPI a second time."""
    usable_w = max(1, area.width - clearance[0])
    usable_h = max(1, area.height - clearance[1])
    return min(width, usable_w, max(1, round(area.width * ratio))), min(height, usable_h, max(1, round(area.height * ratio)))
