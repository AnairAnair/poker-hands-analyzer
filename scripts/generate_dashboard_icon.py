"""
Rasterizes the dashboard icon (src/poker_analyzer/dashboard/assets/icon.svg) to
the PNG sizes app.py actually serves: 32x32 (favicon) and 180x180 (apple-touch-icon).

The project has no SVG-to-PNG rasterizer dependency (cairosvg needs a native cairo
build that's awkward to install on Windows), so this redraws the same shapes with
Pillow instead of parsing the SVG file. The coordinates and colors below are kept
in sync with icon.svg by hand - if you change one, change the other.

Draws at 8x the target size and downsamples with LANCZOS for anti-aliased edges,
since Pillow's ImageDraw has no native anti-aliasing.

The 180x180 output goes to dashboard/static/, not dashboard/assets/, because
app.py references it by URL (via Streamlit's app-static-file serving, enabled
in .streamlit/config.toml) rather than reading it off disk - iOS Safari doesn't
reliably honor a base64-inlined apple-touch-icon. The 32x32 favicon stays in
assets/ since page_icon takes a local path directly, no URL needed.

Run with: python scripts/generate_dashboard_icon.py
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = PROJECT_ROOT / "src" / "poker_analyzer" / "dashboard"
ASSETS_DIR = DASHBOARD_DIR / "assets"
STATIC_DIR = DASHBOARD_DIR / "static"

CHIP_COLOR = "#1a1a1a"
MARK_COLOR = "#ffffff"

SCALE = 8  # supersample factor; final images are downsampled from this


def draw_icon(size: int, opaque_background: str | None = None) -> Image.Image:
    canvas = size * SCALE
    # icon.svg's viewBox is 0-100; map design units to canvas pixels by this
    # factor rather than by SCALE alone, so the drawing fills the canvas at
    # every target size instead of only the fixed 100*SCALE region SCALE alone
    # would produce.
    unit = canvas / 100
    # The chip circle (r=48 of a 0-100 canvas) doesn't reach the square's
    # corners, so without a backdrop those corners are transparent. That's
    # fine for a favicon, but Apple's HIG says apple-touch-icon must have no
    # alpha channel - some iOS versions render a transparent one as a black
    # square or skip it. opaque_background flattens onto a solid color for
    # that case.
    bg = (0, 0, 0, 0) if opaque_background is None else opaque_background
    img = Image.new("RGBA", (canvas, canvas), bg)
    draw = ImageDraw.Draw(img)

    def s(*coords: float) -> list[float]:
        return [c * unit for c in coords]

    def circle(cx: float, cy: float, r: float, fill: str) -> None:
        draw.ellipse(s(cx - r, cy - r, cx + r, cy + r), fill=fill)

    def polygon(points: list[tuple[float, float]], fill: str) -> None:
        flat: list[float] = []
        for x, y in points:
            flat.extend(s(x, y))
        draw.polygon(flat, fill=fill)

    def notch(angle_deg: float) -> None:
        # An 8x9 rounded rect whose own center sits at radius 41 from the chip
        # center (50 - (4 + 9/2) = 41), rotated as a unit (position +
        # orientation) around the chip center by angle_deg - matches
        # icon.svg's rotate(angle 50 50) on a rect at x=46 y=4 w=8 h=9.
        w, h, radius = 8, 9, 2
        local_dim = int(h * unit) + int(8 * unit)
        local = Image.new("RGBA", (local_dim, local_dim), (0, 0, 0, 0))
        local_draw = ImageDraw.Draw(local)
        x0 = (local_dim - w * unit) / 2
        y0 = (local_dim - h * unit) / 2
        local_draw.rounded_rectangle(
            [x0, y0, x0 + w * unit, y0 + h * unit],
            radius=radius * unit,
            fill=MARK_COLOR,
        )
        rotated = local.rotate(-angle_deg, resample=Image.BICUBIC, expand=False)

        rad = math.radians(angle_deg)
        cx = 50 + 41 * math.sin(rad)
        cy = 50 - 41 * math.cos(rad)
        px = cx * unit - local_dim / 2
        py = cy * unit - local_dim / 2
        img.alpha_composite(rotated, (round(px), round(py)))

    # Chip body
    circle(50, 50, 48, CHIP_COLOR)

    # 8 evenly spaced edge notches
    for angle in range(0, 360, 45):
        notch(angle)

    # Mask covering the inner ends of the notches, leaving just a rim tick
    circle(50, 50, 39, CHIP_COLOR)

    # Spade: triangle top + two round lobes + tapered stem, all unioned by fill
    polygon([(50, 24), (28, 58), (72, 58)], MARK_COLOR)
    circle(37, 54, 15, MARK_COLOR)
    circle(63, 54, 15, MARK_COLOR)
    polygon([(50, 56), (42, 82), (58, 82)], MARK_COLOR)

    resized = img.resize((size, size), resample=Image.LANCZOS)
    if opaque_background is not None:
        # Drop the alpha channel entirely (not just fill it to 255) - Apple's
        # HIG says apple-touch-icon should have no alpha channel at all.
        return resized.convert("RGB")
    return resized


def main() -> None:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    # Browser favicons handle transparency fine; apple-touch-icon needs an
    # opaque backdrop (see draw_icon's opaque_background docstring note).
    for size, out_path, background in [
        (32, ASSETS_DIR / "icon-32.png", None),
        (180, STATIC_DIR / "icon-180.png", "#ffffff"),
    ]:
        icon = draw_icon(size, opaque_background=background)
        icon.save(out_path)
        print(f"wrote {out_path} ({size}x{size})")


if __name__ == "__main__":
    main()
