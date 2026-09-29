"""Build the Starfront icon from the Starfront Observatories mark.

    python packaging/make_icon.py [out.ico]

Reads `packaging/logo.png` - the mark, mint on transparent - and writes:

  * the .ico asked for (default `packaging/build/starfront.ico`): the mark on
    a dark disc at 16, 24, 32, 48, 64, 128 and 256 pixels, which is what the
    .exe, the window and the taskbar use;
  * `astrocontrol/web/starfront.ico` and `starfront.png`, the same picture
    for the window's own icon and the page's tab and brand mark, bundled
    with the web folder.

Needs Pillow (python -m pip install pillow). Without it, or without the
logo, falls back to the old hand-drawn star so a build never fails over an
icon - but says so.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LOGO = HERE / "logo.png"
WEB = ROOT / "astrocontrol" / "web"
SIZES = (16, 24, 32, 48, 64, 128, 256)
DISC = (11, 18, 32, 255)                     # the app's own dark blue-black


def from_logo(out: Path) -> bool:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("Pillow is not installed; drawing the fallback icon")
        return False
    if not LOGO.is_file():
        print(f"{LOGO} is missing; drawing the fallback icon")
        return False

    mark = Image.open(LOGO).convert("RGBA")
    # Trim to the mark's own bounds so it sits centred whatever the file's
    # margins were.
    bbox = mark.getbbox()
    if bbox:
        mark = mark.crop(bbox)

    def render(size: int) -> Image.Image:
        # Drawn at 4x and shrunk, for smooth edges at the small sizes.
        big = size * 4
        canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        margin = big * 0.02
        draw.ellipse((margin, margin, big - margin, big - margin), fill=DISC)
        # The mark fills most of the disc; a little more room at the small
        # sizes where a thin tripod would otherwise vanish.
        fill = 0.80 if size >= 48 else 0.86
        span = int(big * fill)
        scale = min(span / mark.width, span / mark.height)
        w, h = max(1, int(mark.width * scale)), max(1, int(mark.height * scale))
        scaled = mark.resize((w, h), Image.LANCZOS)
        canvas.alpha_composite(scaled, ((big - w) // 2, (big - h) // 2))
        return canvas.resize((size, size), Image.LANCZOS)

    frames = [render(size) for size in SIZES]
    out.parent.mkdir(parents=True, exist_ok=True)
    frames[-1].save(out, format="ICO", sizes=[(s, s) for s in SIZES],
                    append_images=frames[:-1])
    WEB.mkdir(parents=True, exist_ok=True)
    frames[-1].save(WEB / "starfront.ico", format="ICO",
                    sizes=[(s, s) for s in SIZES], append_images=frames[:-1])
    render(192).save(WEB / "starfront.png", format="PNG", optimize=True)
    print(f"wrote {out} and {WEB / 'starfront.ico'}, {WEB / 'starfront.png'} "
          f"from {LOGO.name}")
    return True


# ---------------------------------------------------------------- fallback

def star_png(size: int) -> bytes:
    """A size x size RGBA PNG of a four-point star on a dark disc."""
    rows = []
    centre = (size - 1) / 2.0
    radius = size * 0.48
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            dx, dy = x - centre, y - centre
            distance = (dx * dx + dy * dy) ** 0.5
            edge = max(0.0, min(1.0, radius - distance + 0.5))
            axis = min(abs(dx), abs(dy))
            reach = max(abs(dx), abs(dy))
            spike = max(0.0, 1.0 - axis / (size * 0.045)) * max(0.0, 1.0 - reach / (size * 0.42))
            core = max(0.0, 1.0 - distance / (size * 0.11))
            glow = max(spike, core)
            r = int(10 + 245 * glow)
            g = int(12 + 222 * glow)
            b = int(17 + 170 * glow)
            a = int(255 * edge)
            row += bytes((min(255, r), min(255, g), min(255, b), a))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def write_fallback(path: Path, sizes=(16, 32, 48, 256)) -> None:
    images = [(size, star_png(size)) for size in sizes]
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    body = b""
    for size, png in images:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32,
                               len(png), offset)
        body += png
        offset += len(png)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(head + entries + body)
    print(f"wrote {path} (fallback star)")


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else HERE / "build" / "starfront.ico")
    if not from_logo(target):
        write_fallback(target)
