"""Render the out-of-focus circuit board that sits behind every glass surface.

Glass needs something real to look through. Each theme gets a macro photograph that never
existed: one board, lit in that theme's own colors, thrown far out of focus. It is painted
sharp, then defocused in linear light as a lens does, so highlights bloom instead of going
gray, and small lights land as the crisp-edged discs a lens makes of them. Those discs are
what tells the eye this is a photograph and not a gradient.

The output is committed to src/assets/glass/, so the application only loads two small
images per theme and never does any of this work. Rerun it only to change the artwork:

    python -B direct/render_glass_scenes.py [--output DIRECTORY]
"""
from __future__ import annotations

import argparse
from array import array
from dataclasses import dataclass
import math
import os
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPainterPath, QPen, QPolygonF, QTransform

SCENE_WIDTH, SCENE_HEIGHT = 640, 400
FROST_WIDTH, FROST_HEIGHT = 320, 200
# Standard deviations of the two defocus passes, in scene pixels.
LENS_BLUR, FROST_BLUR = 7.5, 8.0


@dataclass(frozen=True)
class Light:
    """Where one out-of-focus point of light lands, as fractions of the frame."""
    x: float
    y: float
    radius: float
    power: float
    ink: str


@dataclass(frozen=True)
class Art:
    board: str
    pour: str
    trace: str
    pin: str
    chip: str
    pool: str
    exposure: float
    # The frosted copy is what text sits on, so its light is held inside what text can take:
    # (luminance where compression starts, luminance it approaches). For the light theme
    # these limit its shadows instead of its highlights.
    frost_knee: float
    frost_limit: float
    lights: tuple[Light, ...]


# Every hue here comes from that theme's palette in main/core/theme.py. Light is what varies,
# not color: the default scene is navy from shadow to highlight, with its cyan in small points.
ART = {
    "default": Art(board="#0c1828", pour="#16283d", trace="#2c4a69", pin="#6d94b8", chip="#070e19",
                   pool="#35587c", exposure=1.5, frost_knee=.045, frost_limit=.082,
                   lights=(Light(.80, .17, .080, .070, "#a7d3f5"), Light(.90, .33, .040, .11, "#80d9ce"),
                           Light(.67, .06, .034, .05, "#cfe6fa"), Light(.10, .82, .085, .050, "#61afef"),
                           Light(.24, .92, .040, .09, "#80d9ce"), Light(.04, .55, .036, .05, "#a7d3f5"),
                           Light(.57, .88, .030, .05, "#a7d3f5"), Light(.95, .78, .060, .06, "#61afef"))),
    "light": Art(board="#c8dbea", pour="#b9cfe2", trace="#a3bfd6", pin="#e6f0f9", chip="#9ab6cd",
                 pool="#eef6fc", exposure=1.0, frost_knee=.62, frost_limit=.40,
                 lights=(Light(.80, .17, .080, .22, "#ffffff"), Light(.90, .33, .040, .20, "#ffffff"),
                         Light(.67, .06, .034, .18, "#ffffff"), Light(.10, .82, .085, .18, "#ffffff"),
                         Light(.24, .92, .040, .16, "#cfe4ff"), Light(.04, .55, .036, .16, "#ffffff"),
                         Light(.57, .88, .030, .16, "#ffffff"), Light(.95, .78, .060, .18, "#ffffff"))),
    "solarized_dark": Art(board="#001d25", pour="#04303b", trace="#0d5463", pin="#5fb0b5", chip="#000a0d",
                          pool="#14606f", exposure=1.45, frost_knee=.045, frost_limit=.082,
                          lights=(Light(.80, .17, .080, .060, "#7fd0ca"), Light(.90, .33, .040, .09, "#b58900"),
                                  Light(.67, .06, .034, .05, "#9fd6d8"), Light(.10, .82, .085, .045, "#268bd2"),
                                  Light(.24, .92, .040, .08, "#2aa198"), Light(.04, .55, .036, .05, "#7fd0ca"),
                                  Light(.57, .88, .030, .05, "#9fd6d8"), Light(.95, .78, .060, .05, "#268bd2"))),
}


def _to_linear(value: float) -> float:
    return value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4


def _to_srgb(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * 12.92 if value <= .0031308 else 1.055 * value ** (1 / 2.4) - .055


def linear_ink(color: str) -> tuple[float, float, float]:
    ink = QColor(color)
    return _to_linear(ink.redF()), _to_linear(ink.greenF()), _to_linear(ink.blueF())


def _routed(start, *moves) -> QPainterPath:
    """A board trace: straight runs joined only at 45 and 90 degrees."""
    path = QPainterPath(QPointF(*start))
    x, y = start
    for dx, dy in moves:
        x, y = x + dx, y + dy
        path.lineTo(x, y)
    return path


def paint_board(art: Art) -> QImage:
    """The board as it would look in focus. Almost none of this detail survives the lens."""
    image = QImage(SCENE_WIDTH, SCENE_HEIGHT, QImage.Format.Format_RGB32)
    image.fill(QColor(art.board))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    width, height = SCENE_WIDTH, SCENE_HEIGHT
    seed = random.Random(0x4d4355)

    # Copper pours: the broad areas of the board that catch more light.
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(art.pour))
    for polygon in (((.0, .46), (.17, .40), (.33, .54), (.33, 1.0), (.0, 1.0)),
                    ((.52, .0), (1.0, .0), (1.0, .42), (.86, .54), (.60, .36)),
                    ((.70, .70), (1.0, .60), (1.0, 1.0), (.62, 1.0)),
                    ((.12, .0), (.36, .0), (.28, .22), (.12, .28))):
        painter.drawPolygon(QPolygonF([QPointF(x * width, y * height) for x, y in polygon]))

    # Daylight falling across the board in one long slanted pool.
    pool = QColor(art.pool)
    pool.setAlphaF(.82)
    painter.setBrush(pool)
    painter.setTransform(QTransform().translate(width * .74, height * .20).rotate(-24))
    painter.drawRoundedRect(QRectF(-width * .30, -height * .15, width * .62, height * .34), 26, 26)
    painter.setTransform(QTransform().translate(width * .16, height * .74).rotate(-24))
    pool.setAlphaF(.56)
    painter.setBrush(pool)
    painter.drawRoundedRect(QRectF(-width * .20, -height * .11, width * .42, height * .26), 22, 22)
    painter.resetTransform()

    # Buses of parallel traces, routed as a board is.
    pen = QPen(QColor(art.trace), 5.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    for offset in range(5):
        step = offset * 13
        painter.drawPath(_routed((-10, 110 + step), (150 - step * .4, 0), (72, 72), (120, 0)))
        painter.drawPath(_routed((650, 262 + step), (-120 - step * .4, 0), (-58, 58), (-90, 0)))
        painter.drawPath(_routed((250 + step, -10), (0, 62), (44, 44), (0, 34)))
    for offset in range(3):
        step = offset * 15
        painter.drawPath(_routed((430 + step, 410), (0, -60), (-40, -40), (0, -20)))
        painter.drawPath(_routed((-10, 330 + step), (70, 0), (30, -30), (60, 0)))

    # Chips: dark blocks whose pins catch the light in a bright dotted edge.
    for left, top, side_x, side_y in ((.40, .36, .24, .36), (.08, .14, .14, .20), (.74, .60, .12, .18)):
        x, y, w, h = left * width, top * height, side_x * width, side_y * height
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(art.pin))
        for along in range(int(w // 11)):
            px = x + 6 + along * 11
            painter.drawRect(QRectF(px, y - 7, 5, 8))
            painter.drawRect(QRectF(px, y + h - 1, 5, 8))
        for along in range(int(h // 11)):
            py = y + 6 + along * 11
            painter.drawRect(QRectF(x - 7, py, 8, 5))
            painter.drawRect(QRectF(x + w - 1, py, 8, 5))
        painter.setBrush(QColor(art.chip))
        painter.drawRoundedRect(QRectF(x, y, w, h), 4, 4)

    # Solder pads and small parts scattered where a real layout would put them.
    painter.setBrush(QColor(art.pin))
    for _ in range(46):
        x, y = seed.uniform(0, width), seed.uniform(0, height)
        if seed.random() < .5:
            painter.drawEllipse(QPointF(x, y), 3.2, 3.2)
        else:
            painter.setBrush(QColor(art.chip))
            painter.drawRect(QRectF(x - 7, y - 4, 14, 8))
            painter.setBrush(QColor(art.pin))
            painter.drawRect(QRectF(x - 9, y - 4, 4, 8))
            painter.drawRect(QRectF(x + 5, y - 4, 4, 8))
    painter.end()
    return image


def _channels(image: QImage) -> list[array]:
    """Three float planes of linear light."""
    table = [_to_linear(level / 255) for level in range(256)]
    planes = [array("d", [0.0]) * (image.width() * image.height()) for _ in range(3)]
    index = 0
    for y in range(image.height()):
        for x in range(image.width()):
            rgb = image.pixel(x, y)
            planes[0][index] = table[(rgb >> 16) & 255]
            planes[1][index] = table[(rgb >> 8) & 255]
            planes[2][index] = table[rgb & 255]
            index += 1
    return planes


def _box_sizes(sigma: float, passes: int = 3) -> list[int]:
    """Box widths whose repeated application approximates this Gaussian."""
    ideal = math.sqrt(12 * sigma * sigma / passes + 1)
    lower = int(ideal) if int(ideal) % 2 else int(ideal) - 1
    upper = lower + 2
    count = round((12 * sigma * sigma - passes * lower * lower - 4 * passes * lower - 3 * passes) / (4 * lower + 4))
    return [lower if index < count else upper for index in range(passes)]


def _box_row(source, target, start, stride, length, reach) -> None:
    """One running-sum box filter along a row or column, holding the edge value past its end."""
    last = length - 1
    total = 0.0
    for offset in range(-reach, reach + 1):
        total += source[start + stride * max(0, min(last, offset))]
    scale = 1 / (2 * reach + 1)
    for position in range(length):
        target[start + stride * position] = total * scale
        total += source[start + stride * min(last, position + reach + 1)]
        total -= source[start + stride * max(0, position - reach)]


def gaussian(planes: list[array], width: int, height: int, sigma: float) -> list[array]:
    result = []
    for plane in planes:
        current = array("d", plane)
        spare = array("d", plane)
        for size in _box_sizes(sigma):
            reach = size // 2
            for row in range(height):
                _box_row(current, spare, row * width, 1, width, reach)
            current, spare = spare, current
            for column in range(width):
                _box_row(current, spare, column, width, height, reach)
            current, spare = spare, current
        result.append(current)
    return result


def add_lights(planes: list[array], width: int, height: int, lights: tuple[Light, ...]) -> None:
    """Point lights through a wide aperture: flat discs with a soft edge, adding to what is there."""
    for light in lights:
        cx, cy, radius = light.x * width, light.y * height, light.radius * width
        ink = linear_ink(light.ink)
        soft = max(3.0, radius * .22)
        left, right = max(0, int(cx - radius - soft)), min(width - 1, int(cx + radius + soft))
        top, bottom = max(0, int(cy - radius - soft)), min(height - 1, int(cy + radius + soft))
        for y in range(top, bottom + 1):
            for x in range(left, right + 1):
                edge = (radius + soft - math.hypot(x - cx, y - cy)) / (2 * soft)
                if edge <= 0:
                    continue
                edge = min(1.0, edge)
                strength = light.power * edge * edge * (3 - 2 * edge)
                index = y * width + x
                for channel in range(3):
                    planes[channel][index] += ink[channel] * strength


def expose(planes: list[array], exposure: float) -> None:
    for plane in planes:
        for index, value in enumerate(plane):
            plane[index] = value * exposure


def hold_light(planes: list[array], knee: float, limit: float) -> None:
    """Compress luminance smoothly past the knee toward the limit, keeping each pixel's hue."""
    red, green, blue = planes
    span = limit - knee
    for index in range(len(red)):
        level = .2126 * red[index] + .7152 * green[index] + .0722 * blue[index]
        # Past the knee means above it, or below it for a limit under the knee.
        beyond = (level - knee) / span
        if beyond <= 0 or level <= 0:
            continue
        scale = (knee + span * (1 - math.exp(-beyond))) / level
        red[index] *= scale
        green[index] *= scale
        blue[index] *= scale


def to_image(planes: list[array], width: int, height: int) -> QImage:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    table_size = 4096
    table = [round(_to_srgb(index / (table_size - 1)) * 255) for index in range(table_size)]
    top = table_size - 1
    index = 0
    red, green, blue = planes
    for y in range(height):
        line = image.scanLine(y)
        row = bytearray(width * 4)
        for x in range(width):
            row[x * 4 + 2] = table[min(top, int(red[index] * top))]
            row[x * 4 + 1] = table[min(top, int(green[index] * top))]
            row[x * 4 + 0] = table[min(top, int(blue[index] * top))]
            row[x * 4 + 3] = 255
            index += 1
        line[:len(row)] = bytes(row)
    return image


def _shrink(planes: list[array], width: int, height: int, new_width: int, new_height: int) -> list[array]:
    """An exact 2:1 reduction, in linear light like everything else here."""
    assert width == new_width * 2 and height == new_height * 2
    result = []
    for plane in planes:
        target = array("d", [0.0]) * (new_width * new_height)
        for y in range(new_height):
            for x in range(new_width):
                first = y * 2 * width + x * 2
                target[y * new_width + x] = (plane[first] + plane[first + 1]
                                             + plane[first + width] + plane[first + width + 1]) / 4
        result.append(target)
    return result


def render(name: str) -> tuple[QImage, QImage]:
    art = ART[name]
    sharp = _channels(paint_board(art))
    lens = gaussian(sharp, SCENE_WIDTH, SCENE_HEIGHT, LENS_BLUR)
    add_lights(lens, SCENE_WIDTH, SCENE_HEIGHT, art.lights)
    # The lens softens even its discs a little, or they would look pasted on.
    lens = gaussian(lens, SCENE_WIDTH, SCENE_HEIGHT, 2.2)
    expose(lens, art.exposure)
    frost = gaussian(lens, SCENE_WIDTH, SCENE_HEIGHT, FROST_BLUR)
    frost = _shrink(frost, SCENE_WIDTH, SCENE_HEIGHT, FROST_WIDTH, FROST_HEIGHT)
    hold_light(frost, art.frost_knee, art.frost_limit)
    return to_image(lens, SCENE_WIDTH, SCENE_HEIGHT), to_image(frost, FROST_WIDTH, FROST_HEIGHT)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "src/assets/glass")
    parser.add_argument("--theme", choices=sorted(ART), action="append")
    arguments = parser.parse_args()
    arguments.output.mkdir(parents=True, exist_ok=True)
    application = QGuiApplication.instance() or QGuiApplication(["render-glass-scenes"])
    for name in arguments.theme or sorted(ART):
        scene, frost = render(name)
        for suffix, image in (("", scene), ("-frosted", frost)):
            target = arguments.output / f"{name}{suffix}.png"
            if not image.save(str(target), "PNG", 0):
                raise SystemExit(f"Could not write {target}")
            print(f"{target}: {image.width()}x{image.height()}, {target.stat().st_size} bytes")
    del application
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
