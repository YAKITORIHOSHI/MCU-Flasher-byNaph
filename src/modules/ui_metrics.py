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


# A half-monitor window is always accepted. Beyond a 1920-wide desktop half is more
# than the workspace needs and would stop third and quarter tiling on wide monitors.
WORKSPACE_MINIMUM_WIDTH_CAP = 960


def workspace_minimum_width(area_width):
    """Smallest complete frame: half the monitor, capped on very wide desktops."""
    return max(1, min(int(area_width) // 2, WORKSPACE_MINIMUM_WIDTH_CAP))


def workspace_size(area: WorkArea):
    """Default workspace size for any monitor, in the work area's own units."""
    width, height = area.width, area.height
    if width < 1280 or height < 800:
        # Laptops at high display scale: every line of code counts.
        return max(1, width - 16), max(1, height - 34)
    # Ordinary desktops keep 1440x920. Large, ultrawide and portrait monitors
    # grow proportionally, up to a size that is still comfortable to read across.
    target_width = min(max(1440, round(width * .75)), 2560)
    target_height = min(max(920, round(height * .85)), 1440)
    return preferred_size(area, target_width, target_height, ratio=.90)
