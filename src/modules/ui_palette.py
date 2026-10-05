"""Small, toolkit-independent color helpers for native glass surfaces."""


def mix(first: str, second: str, amount: float) -> str:
    a = tuple(int(first[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(second[i:i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(a, b))


def readable_foreground(background: str) -> str:
    """Choose dark or white button text by relative luminance."""
    rgb = [int(background[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in rgb]
    luminance = sum(v * w for v, w in zip(linear, (.2126, .7152, .0722)))
    return "#ffffff" if luminance < .179 else "#000000"


def contrast_ratio(foreground: str, background: str) -> float:
    """Relative-luminance contrast without loading either GUI toolkit."""
    def luminance(color):
        channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        return sum((v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4) * weight
                   for v, weight in zip(channels, (.2126, .7152, .0722)))
    high, low = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (high + .05) / (low + .05)


def setup_text_palette(palette: dict[str, str], *, glass: bool = False) -> dict[str, str]:
    """Copy setup inks and minimally adjust them for the static reading surfaces.

    Glass body gradients interpolate between these stops; reflections add at
    most 46/255 white in dark themes or 92/255 in light themes. Checking both
    bounds keeps text readable through resizing without sampling or repaint work.
    Backgrounds, borders and the shared workspace palette remain untouched.
    """
    result = dict(palette)
    stops = [palette["T_" + key] for key in ("BG_DARKEST", "BG_DARK", "BG_MID", "BG_LIGHT")]
    backgrounds = [*stops, palette["T_BG_HOVER"]]
    if glass:
        light = readable_foreground(palette["T_BG_DARKEST"]) == "#000000"
        backgrounds.extend(mix(color, "#ffffff", (92 if light else 46) / 255) for color in stops)
    for key in ("T_TEXT", "T_TEXT_DIM", "T_TEXT_BRIGHT", "T_CYAN", "T_GREEN", "T_YELLOW", "T_RED", "T_MAGENTA"):
        color = palette[key]
        def readable(candidate):
            return all(contrast_ratio(candidate, background) >= 4.5 for background in backgrounds)
        if readable(color):
            continue
        candidates = []
        for endpoint in ("#000000", "#ffffff"):
            if not readable(endpoint):
                continue
            low, high = 0.0, 1.0
            for _ in range(24):
                middle = (low + high) / 2
                if readable(mix(color, endpoint, middle)):
                    high = middle
                else:
                    low = middle
            candidates.append((high, mix(color, endpoint, high)))
        if candidates:
            result[key] = min(candidates)[1]
    return result
