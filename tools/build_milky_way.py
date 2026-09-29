"""Build the Milky Way backdrop the Planetarium draws behind the stars.

    python tools/build_milky_way.py [path-to-eso0932a.jpg]

Source: ESO/S. Brunier, "The Milky Way panorama" (eso0932a), CC BY 4.0,
https://www.eso.org/public/images/eso0932a/ - a 360-degree photographic
panorama in galactic coordinates, equirectangular, the galactic centre in
the middle and longitude increasing to the left, the way the sky reads.

What comes out is `astrocontrol/web/vendor/milkyway.png`: the same map at
half a degree per pixel (720 x 360), colour kept, with the stars taken out
by a block median so only the diffuse glow remains - the chart draws its
own stars from the catalogue. Regenerating needs Pillow and the ESO file,
which is why the result is committed and this is a one-off tool.
"""
import sys
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "astrocontrol" / "web" / "vendor" / "milkyway.png"
SOURCE = "https://cdn.eso.org/images/large/eso0932a.jpg"
COLUMNS, ROWS = 720, 360


def main() -> None:
    from PIL import Image

    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        path = Path(__file__).resolve().parent / "eso0932a.jpg"
        if not path.is_file():
            print(f"fetching {SOURCE}")
            urllib.request.urlretrieve(SOURCE, path)

    image = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32)
    height, width, _ = image.shape
    print(f"source {width}x{height}")

    # Block median: a star is a few bright pixels in a block of many, so the
    # median keeps the glow and drops the star. Blocks are the source size
    # over the output size, floored, so a 6000 x 3000 source uses 8 x 8.
    bw, bh = width // COLUMNS, height // ROWS
    trimmed = image[:bh * ROWS, :bw * COLUMNS]
    blocks = trimmed.reshape(ROWS, bh, COLUMNS, bw, 3).transpose(0, 2, 1, 3, 4)
    blocks = blocks.reshape(ROWS, COLUMNS, bh * bw, 3)
    glow = np.median(blocks, axis=2)

    # The floor is the sky between the clouds, which should draw as nothing
    # at all: subtract it, then a gentle stretch so the faint outer arms
    # survive the cut and the bulge does not saturate.
    floor = np.percentile(glow, 8, axis=(0, 1))
    glow = np.clip(glow - floor, 0, None)
    top = np.percentile(glow, 99.7)
    glow = np.clip(glow / max(top, 1e-6), 0, 1) ** 0.75
    out = (glow * 255).astype(np.uint8)

    # The brightest few stars leave a glare bigger than a block, and the
    # median keeps it as a bright smudge: a cell well above its neighbours
    # is one of those, and takes its neighbours' value instead. The chart
    # draws Sirius itself.
    from PIL import ImageFilter
    first = Image.fromarray(out, "RGB")
    neighbours = np.asarray(first.filter(ImageFilter.MedianFilter(9)), dtype=np.float32)
    lum = out.astype(np.float32).mean(axis=2)
    around = neighbours.mean(axis=2)
    glare = lum > np.maximum(around * 1.6, around + 18)
    cleaned = np.where(glare[..., None], neighbours, out.astype(np.float32))
    print(f"glare cells replaced: {int(glare.sum())}")

    # A light blur so the half-degree cells do not read as tiles.
    result = Image.fromarray(cleaned.astype(np.uint8), "RGB").filter(
        ImageFilter.GaussianBlur(0.8))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    result.save(OUT, optimize=True)
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
