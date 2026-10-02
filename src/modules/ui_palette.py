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
