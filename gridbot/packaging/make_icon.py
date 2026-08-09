"""make_icon.py — generate gridbot/packaging/gridbot.ico with Pillow.

Design: dark rounded square, horizontal grid levels, price zigzag on top,
green (buy / up-leg) and red (sell / down-leg) accents.

Output: multi-size .ico (16, 24, 32, 48, 64, 128, 256), written next to
this script regardless of the current working directory.

Usage:  python gridbot/packaging/make_icon.py
"""

from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ICO_PATH = os.path.join(HERE, "gridbot.ico")

SIZES = [16, 24, 32, 48, 64, 128, 256]

# Palette
BG = (18, 23, 33, 255)          # dark navy background
BG_EDGE = (46, 58, 78, 255)     # subtle border
GRID = (58, 72, 96, 255)        # muted grid lines
GREEN = (46, 204, 113, 255)     # up-leg / buy accent
RED = (231, 76, 60, 255)        # down-leg / sell accent
DOT_RING = (18, 23, 33, 255)    # ring around dots (background color)


def draw_master(canvas: int = 1024) -> Image.Image:
    """Draw the icon at high resolution on a transparent canvas."""
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # --- rounded dark square ---
    m = 32                      # outer margin
    radius = 180
    d.rounded_rectangle((m, m, canvas - m, canvas - m), radius=radius, fill=BG)
    d.rounded_rectangle(
        (m, m, canvas - m, canvas - m), radius=radius, outline=BG_EDGE, width=14
    )

    # --- horizontal grid levels ---
    x0, x1 = 120, canvas - 120
    for y in (220, 330, 440, 550, 660, 770):
        d.line((x0, y, x1, y), fill=GRID, width=10)

    # --- price zigzag ---
    pts = [
        (110, 660),
        (280, 430),
        (420, 560),
        (580, 300),
        (720, 470),
        (915, 215),
    ]
    lw = 40
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        color = GREEN if by < ay else RED   # up-leg green, down-leg red
        d.line((ax, ay, bx, by), fill=color, width=lw)
        # round the joints so the polyline looks smooth
        r = lw // 2
        d.ellipse((bx - r, by - r, bx + r, by + r), fill=color)
    ax, ay = pts[0]
    r = lw // 2
    d.ellipse((ax - r, ay - r, ax + r, ay + r), fill=GREEN)

    # --- buy/sell dots at interior vertices ---
    # local price highs -> red (sell), local lows -> green (buy)
    for i in range(1, len(pts) - 1):
        px, py = pts[i]
        is_high = py < pts[i - 1][1] and py < pts[i + 1][1]
        color = RED if is_high else GREEN
        r_out, r_in = 62, 44
        d.ellipse((px - r_out, py - r_out, px + r_out, py + r_out), fill=DOT_RING)
        d.ellipse((px - r_in, py - r_in, px + r_in, py + r_in), fill=color)

    return img


def main() -> int:
    master = draw_master(1024)
    base = master.resize((256, 256), Image.LANCZOS)
    frames = {s: master.resize((s, s), Image.LANCZOS) for s in SIZES}

    base.save(
        ICO_PATH,
        format="ICO",
        sizes=[(s, s) for s in SIZES],
        append_images=[frames[s] for s in SIZES if s != 256],
    )

    # sanity check: reopen and verify
    with Image.open(ICO_PATH) as ico:
        if ico.format != "ICO":
            print("ERROR: written file is not a valid ICO", file=sys.stderr)
            return 1
        got = sorted(ico.info.get("sizes", set()))
    expected = sorted((s, s) for s in SIZES)
    if got != expected:
        print(f"ERROR: ICO sizes mismatch: {got} != {expected}", file=sys.stderr)
        return 1

    print(f"OK: {ICO_PATH} ({os.path.getsize(ICO_PATH)} bytes, sizes={got})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
