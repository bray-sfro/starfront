"""Taking the sky off a frame: a level, or a tilted plane.

Every frame arrives sitting on a background that is not the object. Light
pollution from one direction, the Moon, twilight, a dew heater glow, amp glow
the darks did not quite catch — and on a wide field none of that is flat. It
is a ramp across the frame, brightest towards the town, and it is *additive*,
which is what makes it a different problem from vignetting: a flat divides,
a gradient subtracts, and neither fixes the other.

For a stack of several telescopes it has to come off before anything is
combined, and it has to come off *per frame*. Two observatories under
different skies have different ramps in different directions, and averaging
them produces a ramp that is nobody's — one that no later gradient removal
can find, because it is no longer a plane. Take each frame's own ramp off as
it arrives and what is left is the sky, which is the same everywhere.

**First order, and no higher, on purpose.** A plane has three numbers and
cannot do much harm. The moment a background model can bend, it can bend
around the object and quietly subtract it, and on a live stack — where
nobody is watching each frame go by and there is no undo — that is not a
trade worth making. Real gradients from light pollution over the few degrees
a telescope sees genuinely are close to planar; the curved ones come from
optics, and those belong to the flat.

**The fit is on tiles, and rejection is asymmetric.** That second part is
what makes it safe. Nebulosity, galaxies and stars all add light and none of
them removes any, so a tile sitting well *above* the fitted plane is probably
object and is dropped, while one below it is probably just a dark patch of
sky and is kept. A symmetric clip — the obvious thing to write — pulls the
plane up into the nebula on one side and then throws away the honest sky on
the other to compensate, which tilts the answer in the one direction nobody
would notice.

There is a last rail: a plane that varies across the frame by more than the
sky level itself is not describing a gradient, it is describing the object.
That one falls back to a plain level and says so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

#: Roughly how many tiles across the frame's long axis. Enough that a real
#: ramp is sampled well and an object covers only some of them; few enough
#: that each tile still holds thousands of pixels and its median means
#: something.
TILES_ACROSS = 40

#: The fit is made on a subsample of the frame. A sixty-megapixel sub does
#: not need every pixel to have its sky measured — a few million settles a
#: tile median to a small fraction of an ADU — and taking every pixel makes
#: this the slowest thing in the pipeline for no gain at all.
SAMPLE_PIXELS = 4_000_000

#: How far above the fitted surface a tile may sit before it is taken as
#: object rather than sky, in robust deviations of the residuals.
REJECT_HIGH = 2.0

#: And how far below. Deliberately looser: a tile darker than the plane is a
#: dark patch of sky, which is exactly the thing being measured.
REJECT_LOW = 4.0

#: Fit, reject, fit again, until the set of tiles being kept stops changing.
#:
#: The cap is high because the first pass is made over every tile including
#: the object's, so the first surface is the worst one and the early passes
#: are spent walking off it. Measured on a synthetic frame with a nebula five
#: times the sky brightness in one corner: three passes left the slope 16%
#: wrong, five left it 10% wrong, eight left it 1%, and it is under half a
#: per cent by twelve. Each pass is a least-squares fit of three unknowns to
#: a couple of thousand tiles — microseconds — so the cap costs nothing and
#: the early exit means a clean frame pays for two.
PASSES = 12

#: The fewest tiles worth fitting a plane to. Below this the answer is being
#: driven by a handful of samples and a level is the honest model.
MIN_TILES = 24

#: How much of the sky level a fitted plane may span across the frame before
#: it is disbelieved. At 1.0 the model says the sky reaches zero at one edge,
#: which no real gradient does — past that it has found the object.
MAX_SPAN = 1.0

#: Bands used when the surface is subtracted, in pixels. A plane over a
#: sixty-megapixel frame is another sixty megapixels of float, and building
#: it whole doubles the peak memory of the pipeline for one subtraction.
BAND_ROWS = 512


@dataclass(frozen=True)
class Surface:
    """A fitted background: `value = a*x + b*y + c` in zero-based pixels.

    Degree zero leaves `a` and `b` at zero, so one shape describes both
    models and nothing downstream has to ask which it got.
    """

    a: float = 0.0                 # ADU per pixel across
    b: float = 0.0                 # ADU per pixel down
    c: float = 0.0                 # ADU at pixel (0, 0)
    degree: int = 0
    level: float = 0.0             # the sky at the middle of the frame
    noise: float = 0.0             # robust scatter of the sky about the fit
    span: float = 0.0              # how much the plane varies across the frame
    tiles: int = 0                 # tiles the fit was made on
    detail: str = ""

    def at(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        return self.a * x + self.b * y + self.c

    def payload(self) -> dict[str, Any]:
        return {
            "degree": self.degree,
            "level": round(self.level, 4),
            "noise": round(self.noise, 4),
            "span": round(self.span, 4),
            "spanPercent": (round(100.0 * self.span / self.level, 2)
                            if self.level > 0 else None),
            "slopeX": self.a,
            "slopeY": self.b,
            "tiles": self.tiles,
            "detail": self.detail,
        }


def _samples(frame: np.ndarray, mask: np.ndarray | None
             ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """A robust sky estimate per tile, with each tile's centre in frame pixels.

    The median of a tile rather than its mean, because a tile with a star in
    it has a mean the star decides and a median it does not. Tiles that are
    not wholly inside the frame's real data are dropped rather than
    part-measured: a tile half off the edge of a reprojected frame has half
    its pixels at zero, and zero is not a dark sky.
    """
    height, width = frame.shape
    # Subsample first, so the medians are taken over a few million pixels
    # rather than all sixty.
    stride = max(1, int(math.sqrt(frame.size / SAMPLE_PIXELS)))
    small = frame[::stride, ::stride]
    small_mask = None if mask is None else mask[::stride, ::stride]

    tile = max(4, min(small.shape) // TILES_ACROSS)
    rows = small.shape[0] // tile
    columns = small.shape[1] // tile
    if rows < 2 or columns < 2:
        return (np.empty(0), np.empty(0), np.empty(0))

    trimmed = small[:rows * tile, :columns * tile]
    blocks = (trimmed.reshape(rows, tile, columns, tile)
              .transpose(0, 2, 1, 3).reshape(rows, columns, tile * tile))
    values = np.median(blocks, axis=2)

    keep = np.isfinite(values)
    if small_mask is not None:
        covered = (small_mask[:rows * tile, :columns * tile]
                   .reshape(rows, tile, columns, tile)
                   .transpose(0, 2, 1, 3).reshape(rows, columns, tile * tile))
        keep &= covered.all(axis=2)

    # Tile centres, back in the full frame's own zero-based pixels.
    centre = (tile - 1) / 2.0
    ys = (np.arange(rows) * tile + centre) * stride
    xs = (np.arange(columns) * tile + centre) * stride
    grid_x, grid_y = np.meshgrid(xs, ys)
    return (grid_x[keep].astype(np.float64),
            grid_y[keep].astype(np.float64),
            values[keep].astype(np.float64))


def _lower_scale(residual: np.ndarray, middle: float) -> float:
    """The scatter of the sky, measured from below the surface only.

    The ordinary median absolute deviation is taken about the middle and
    looks both ways, which makes it useless here in a way that is easy to
    miss: the tiles sitting on a nebula are exactly the ones being hunted,
    and while they are still in the sample they inflate the very number the
    rejection threshold is built from. The result is a threshold too generous
    to catch them, and a plane that leans several per cent into the object —
    measured at sixteen per cent of the slope on a frame with one modest
    nebula in a corner.

    Below the surface there is nothing but sky and noise, because everything
    astronomical adds light. So the spread is measured there and doubled by
    symmetry, which gives a scale the object cannot touch.
    """
    below = residual[residual < middle]
    if below.size < 8:
        spread = float(np.median(np.abs(residual - middle)))
    else:
        spread = float(np.median(middle - below))
    return spread * 1.4826


def measure(frame: np.ndarray, mask: np.ndarray | None = None,
            degree: int = 1) -> Surface:
    """Fit the background of a frame, as a level or as a tilted plane.

    Returns a `Surface` whatever happens — a frame too small, too empty or
    too full of object still gets a level, because something has to come off
    before a frame can be stacked and a constant is always defensible.
    `detail` says which model was used and why.
    """
    if frame.ndim != 2 or frame.size == 0:
        return Surface(detail="the frame is empty")

    xs, ys, values = _samples(frame, mask)
    if len(values) < MIN_TILES:
        level, noise = _level(frame, mask)
        return Surface(c=level, degree=0, level=level, noise=noise,
                       tiles=len(values),
                       detail=(f"only {len(values)} usable tiles, so a level "
                               "was taken rather than a plane"))

    level = float(np.median(values))
    scatter = float(np.median(np.abs(values - level))) * 1.4826
    if degree < 1:
        return Surface(c=level, degree=0, level=level, noise=scatter,
                       tiles=len(values), detail="a level, as asked")

    keep = np.ones(len(values), dtype=bool)
    coefficients = np.array([0.0, 0.0, level])
    for _ in range(PASSES):
        if int(keep.sum()) < MIN_TILES:
            break
        design = np.column_stack([xs[keep], ys[keep],
                                  np.ones(int(keep.sum()))])
        try:
            coefficients, *_ = np.linalg.lstsq(design, values[keep], rcond=None)
        except np.linalg.LinAlgError:              # pragma: no cover
            break
        residual = values - (coefficients[0] * xs + coefficients[1] * ys
                             + coefficients[2])
        middle = float(np.median(residual[keep]))
        spread = _lower_scale(residual[keep], middle)
        if spread <= 0:
            break
        # Asymmetric on purpose: everything astronomical adds light, so a
        # tile above the surface is suspected of holding an object and one
        # below it is simply darker sky.
        settled = ((residual - middle) < REJECT_HIGH * spread) & \
                  ((residual - middle) > -REJECT_LOW * spread)
        if np.array_equal(settled, keep):
            break                              # nothing moved; it has converged
        keep = settled

    a, b, c = (float(coefficients[0]), float(coefficients[1]),
               float(coefficients[2]))
    height, width = frame.shape
    corners = [a * x + b * y + c
               for x in (0.0, width - 1.0) for y in (0.0, height - 1.0)]
    span = max(corners) - min(corners)
    middle = a * (width - 1) / 2.0 + b * (height - 1) / 2.0 + c
    residual = values - (a * xs + b * ys + c)
    noise = float(np.median(np.abs(residual - np.median(residual)))) * 1.4826

    if middle <= 0 or span > MAX_SPAN * abs(middle):
        # A plane that runs the sky down to nothing across the frame is not a
        # gradient. Far more likely the object fills the frame and the fit has
        # leaned on it; a level cannot make that mistake.
        return Surface(c=level, degree=0, level=level, noise=scatter,
                       tiles=int(keep.sum()),
                       detail=(f"the fitted plane varied by {span:.0f} ADU "
                               f"across a sky of {middle:.0f}, which is the "
                               "object rather than a gradient - a level was "
                               "taken instead"))

    return Surface(a=a, b=b, c=c, degree=1, level=float(middle), noise=noise,
                   tiles=int(keep.sum()), span=float(span),
                   detail=(f"a plane over {int(keep.sum())} tiles: sky "
                           f"{middle:.1f} ADU, tilted by {span:.1f} ADU "
                           f"({100.0 * span / middle:.1f}%) across the frame"))


def _level(frame: np.ndarray, mask: np.ndarray | None
           ) -> tuple[float, float]:
    """The flat fallback: a median and a robust scatter, cheaply."""
    data = frame[mask] if mask is not None else frame.reshape(-1)
    if data.size == 0:
        return 0.0, 0.0
    if data.size > SAMPLE_PIXELS:
        data = data[:: max(1, data.size // SAMPLE_PIXELS)]
    level = float(np.median(data))
    return level, float(np.median(np.abs(data - level))) * 1.4826


def remove(frame: np.ndarray, mask: np.ndarray | None = None,
           degree: int = 1) -> tuple[np.ndarray, Surface]:
    """Take the fitted background off a frame. Returns float32 and the fit.

    The result is centred on zero rather than on the sky: what is left is
    signal above the background, which is what a stack of several telescopes
    can actually add together. Values genuinely go negative and are left
    that way — clipping at zero here would bias the sky of every frame
    upwards by half its noise, and a stack of a hundred such frames has a
    background that is a measurable amount too bright.

    Subtracted in bands, because building a plane the size of a
    sixty-megapixel frame doubles the peak memory of the pipeline for one
    subtraction.
    """
    surface = measure(frame, mask, degree)
    height, width = frame.shape
    out = np.empty(frame.shape, dtype=np.float32)
    columns = np.arange(width, dtype=np.float64)
    across = surface.a * columns + surface.c
    for start in range(0, height, BAND_ROWS):
        stop = min(height, start + BAND_ROWS)
        rows = np.arange(start, stop, dtype=np.float64)
        band = across[None, :] + (surface.b * rows)[:, None]
        out[start:stop] = (frame[start:stop].astype(np.float32)
                           - band.astype(np.float32))
    return out, surface
