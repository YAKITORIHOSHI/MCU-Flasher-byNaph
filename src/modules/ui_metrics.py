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


# Wayland never tells a client where docks and panels are, so Qt reports the
# whole monitor as available. GNOME then tiles half of what the dock leaves
# (Ubuntu's dock is 60-90 logical pixels wide), and refuses to tile a window
# whose minimum is wider than that half. Allow for an unreported side panel.
UNREPORTED_PANEL_ALLOWANCE = 128


def tiling_area_width(area_width, panels_reported=True):
    """The width a desktop divides when it tiles windows side by side."""
    width = max(1, int(area_width))
    if panels_reported:
        return width
    return max(1, width - min(UNREPORTED_PANEL_ALLOWANCE, width // 8))


# The shortest complete frame, as a share of the monitor's usable height. Shorter
# windows left one line of code above the tools, which read as a broken layout.
WORKSPACE_MINIMUM_HEIGHT_SHARE = 0.75


def workspace_minimum_height(area_height):
    """Smallest complete frame: three quarters of the monitor's usable height."""
    return max(1, round(int(area_height) * WORKSPACE_MINIMUM_HEIGHT_SHARE))


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


# The logical pixel density toolkits assume. Above it a logical pixel is physically small.
_NOMINAL_PIXELS_PER_INCH = 96


def interface_scale(width, height, pixels_per_inch, device_ratio=1.0):
    """Extra scale this monitor calls for, from logical units with no scale of ours applied.

    A display scale chosen for another screen, or left at 100%, can leave a lot of room in
    pixels too small to read: a small 1080p laptop, or 4K on a desk. Its opposite is a small
    desktop at a high scale, where the chrome crowds out the code and can afford to shrink
    because it still has plenty of device pixels. Everything else is left alone.
    """
    density = pixels_per_inch / _NOMINAL_PIXELS_PER_INCH if 40 <= pixels_per_inch <= 600 else 1.0
    if width >= 1700 and height >= 950:
        return 1.25 if density >= 1.6 else 1.125 if density >= 1.3 else 1.0
    if device_ratio >= 1.5 and (width < 1000 or height < 560):
        return .875
    return 1.0
