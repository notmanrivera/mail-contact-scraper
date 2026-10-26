"""Draw the app icon (red tile, white envelope) and the menu bar icon with Pillow.

    python packaging/make_icon.py build/icon      # -> build/icon/AppIcon.iconset/, menubar.png
    iconutil -c icns build/icon/AppIcon.iconset   # macOS only
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

RED = (168, 24, 24, 255)


def envelope(draw, box, color, width):
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, radius=(x1 - x0) * 0.08, outline=color, width=width)
    mid = ((x0 + x1) / 2, y0 + (y1 - y0) * 0.55)
    draw.line([(x0 + width, y0 + width), mid, (x1 - width, y0 + width)], fill=color, width=width, joint="curve")


def app_icon(size):
    scale = 4  # draw big, shrink: smooth edges
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = s * 0.1  # macOS icons leave a margin around the tile
    d.rounded_rectangle((pad, pad, s - pad, s - pad), radius=s * 0.18, fill=RED)
    envelope(d, (s * 0.25, s * 0.33, s * 0.75, s * 0.67), (255, 255, 255, 255), max(1, int(s * 0.04)))
    return img.resize((size, size), Image.LANCZOS)


def menubar_icon(size=40):
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    envelope(ImageDraw.Draw(img), (s * 0.1, s * 0.22, s * 0.9, s * 0.78), (0, 0, 0, 255), int(s * 0.08))
    return img.resize((size, size), Image.LANCZOS)


def main(out):
    out = Path(out)
    iconset = out / "AppIcon.iconset"
    iconset.mkdir(parents=True, exist_ok=True)
    for base in (16, 32, 128, 256, 512):
        app_icon(base).save(iconset / f"icon_{base}x{base}.png")
        app_icon(base * 2).save(iconset / f"icon_{base}x{base}@2x.png")
    menubar_icon().save(out / "menubar.png")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "build/icon")
