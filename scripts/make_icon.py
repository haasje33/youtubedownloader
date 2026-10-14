"""Draw the app icon and, on macOS, turn it into an .icns file.

    python scripts/make_icon.py OUTPUT_DIR

Writes OUTPUT_DIR/AppIcon.png (1024px) and, when `iconutil` is available,
OUTPUT_DIR/AppIcon.icns.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZE = 1024


def vertical_gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    img = Image.new("RGBA", (size, size))
    draw = ImageDraw.Draw(img)
    for y in range(size):
        t = y / (size - 1)
        color = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,)
        draw.line([(0, y), (size, y)], fill=color)
    return img


def draw_icon() -> Image.Image:
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # macOS icon grid: ~824px rounded square centred on a 1024 canvas.
    inset, radius = 100, 185
    box = (inset, inset, SIZE - inset, SIZE - inset)

    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (box[0], box[1] + 14, box[2], box[3] + 14), radius, fill=(0, 0, 0, 110))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18)))

    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius, fill=255)
    background = vertical_gradient(SIZE, (255, 72, 66), (196, 18, 28))
    canvas.paste(background, (0, 0), mask)

    draw = ImageDraw.Draw(canvas)
    white = (255, 255, 255, 255)
    cx = SIZE // 2

    # Downward arrow.
    shaft_w, shaft_top, shaft_bottom = 110, 250, 560
    draw.rounded_rectangle((cx - shaft_w // 2, shaft_top, cx + shaft_w // 2, shaft_bottom),
                           radius=40, fill=white)
    head_w, head_top, head_tip = 380, 470, 700
    draw.polygon([(cx - head_w // 2, head_top), (cx + head_w // 2, head_top), (cx, head_tip)], fill=white)

    # Tray underneath.
    tray_y, tray_w, tray_h = 760, 500, 64
    draw.rounded_rectangle((cx - tray_w // 2, tray_y, cx + tray_w // 2, tray_y + tray_h),
                           radius=tray_h // 2, fill=white)
    return canvas


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "build/icon")
    out.mkdir(parents=True, exist_ok=True)
    icon = draw_icon()
    icon.save(out / "AppIcon.png")

    if not shutil.which("iconutil"):
        print(f"Wrote {out / 'AppIcon.png'} (iconutil not available, skipping .icns)")
        return 0

    iconset = out / "AppIcon.iconset"
    iconset.mkdir(exist_ok=True)
    for size in (16, 32, 128, 256, 512):
        icon.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
        icon.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out / "AppIcon.icns")], check=True)
    print(f"Wrote {out / 'AppIcon.icns'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
